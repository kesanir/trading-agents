"""
services/trading_runner.py
Workflow: fetch → parse → detect structures → evaluate → execute → persist.
Closes are only attempted during market hours unless SCAN_OUTSIDE_MARKET_HOURS=true.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from alpaca.trading.client import TradingClient

from core.config import settings
from core.logging import get_logger
from core.market_hours import is_market_open, session_label
from core.models import (
    CalendarSpread, CreditSpread, IronCondor, MultiLegStructure,
    Position, RiskAssessment, RunRecord,
)
from app.data.alpaca_trading import (
    cancel_order,
    fetch_account,
    fetch_all_positions,
    fetch_open_orders,
    get_trading_client,
)
from app.db.run_store import init_db, make_run_id, save_run_record
from app.features.position_parser import detect_structures, parse_all_positions
from app.features.risk_evaluator import evaluate_single_position, evaluate_structure
from app.services.close_orchestrator import execute_risk_assessment

logger = get_logger(__name__)


@dataclass
class RunnerReport:
    run_id: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    session: str = ""
    account_equity: float = 0.0
    account_buying_power: float = 0.0
    total_positions: int = 0
    structures: list[MultiLegStructure] = field(default_factory=list)
    orphans: list[Position] = field(default_factory=list)
    assessments: list[RiskAssessment] = field(default_factory=list)
    actions_taken: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    closes_deferred: int = 0   # flagged but not submitted (market closed)

    def structure_counts(self) -> dict:
        c: Counter = Counter()
        for s in self.structures:
            c[type(s).__name__] += 1
        return dict(c)

    def closes_triggered(self) -> int:
        return sum(1 for a in self.assessments if a.should_close)

    def to_run_record(self) -> RunRecord:
        return RunRecord(
            run_id=self.run_id,
            timestamp=self.timestamp,
            session=self.session,
            account_equity=self.account_equity,
            account_buying_power=self.account_buying_power,
            total_positions=self.total_positions,
            structures_found=self.structure_counts(),
            assessments_count=len(self.assessments),
            closes_triggered=self.closes_triggered(),
            errors=self.errors,
            actions=self.actions_taken,
        )


async def _cancel_pending_for_symbols(
    client: TradingClient, symbols: set[str], open_orders: list[dict],
) -> None:
    if settings.DRY_RUN:
        logger.info('Dry run: pending orders will not be cancelled.')
        return
    for order in open_orders:
        if (
            order.get("symbol") in symbols
            and order.get("status") in ("new", "partially_filled", "accepted", "pending_new")
        ):
            try:
                await cancel_order(client, str(order["id"]))
            except Exception as exc:
                logger.warning("cancel_order %s failed: %s", order.get("id"), exc)


def _symbols_for_close(assessments: list[RiskAssessment]) -> set[str]:
    symbols: set[str] = set()
    for a in assessments:
        if not a.should_close:
            continue
        if a.structure is not None:
            for leg in a.structure.all_legs():
                symbols.add(leg.position.symbol)
        elif a.orphan_position:
            symbols.add(a.orphan_position.symbol)
    return symbols


async def run_once() -> RunnerReport:
    report = RunnerReport(
        run_id=make_run_id(),
        timestamp=datetime.utcnow(),
        session=session_label(),
    )
    client: TradingClient = get_trading_client()

    await init_db()

    # ── 1. Account ────────────────────────────────────────────────────────────
    try:
        acct = await fetch_account(client)
        report.account_equity       = float(acct.get("equity", 0))
        report.account_buying_power = float(acct.get("buying_power", 0))
        logger.info(
            "Account: equity=%.2f buying_power=%.2f session=%s",
            report.account_equity, report.account_buying_power, report.session,
        )
    except Exception as exc:
        report.errors.append(f"fetch_account: {exc}")
        logger.error("fetch_account failed: %s", exc)
        return report

    # ── 2. Positions ──────────────────────────────────────────────────────────
    try:
        raw_positions = await fetch_all_positions(client)
    except Exception as exc:
        report.errors.append(f"fetch_positions: {exc}")
        logger.error("fetch_positions failed: %s", exc)
        return report

    if not raw_positions:
        logger.info("No open positions.")
        await save_run_record(report.to_run_record())
        return report

    positions = parse_all_positions(raw_positions)
    report.total_positions = len(positions)

    # ── 3. Structure detection ────────────────────────────────────────────────
    structures, orphans = detect_structures(positions)
    report.structures = structures
    report.orphans    = orphans
    struct_summary = report.structure_counts()
    logger.info(
        "Structures detected: %s | Orphans: %d",
        struct_summary, len(orphans),
    )

    # ── 4. Open orders (pre-close cancellation) ───────────────────────────────
    try:
        open_orders = await fetch_open_orders(client)
    except Exception as exc:
        logger.warning("fetch_open_orders failed: %s", exc)
        open_orders = []

    # ── 5. Risk evaluation ────────────────────────────────────────────────────
    assessments: list[RiskAssessment] = []

    for structure in structures:
        try:
            a = evaluate_structure(structure)
            _log_assessment(a)
            assessments.append(a)
        except Exception as exc:
            err = f"evaluate_structure({type(structure).__name__}): {exc}"
            report.errors.append(err)
            logger.error(err)

    for pos in orphans:
        try:
            a = evaluate_single_position(pos)
            _log_assessment(a)
            assessments.append(a)
        except Exception as exc:
            err = f"evaluate_position({pos.symbol}): {exc}"
            report.errors.append(err)
            logger.error(err)

    report.assessments = assessments

    # ── 6. Market hours gate ──────────────────────────────────────────────────
    market_live = is_market_open() or settings.SCAN_OUTSIDE_MARKET_HOURS
    if not market_live:
        flagged = [a for a in assessments if a.should_close]
        report.closes_deferred = len(flagged)
        if flagged:
            logger.warning(
                "Market closed — %d position(s) flagged for close will execute at next market-open scan: %s",
                len(flagged),
                [a.symbol for a in flagged],
            )
        else:
            logger.info("Market closed — no positions require closing.")
        await save_run_record(report.to_run_record())
        return report

    # ── 7. Pre-close: cancel pending orders ───────────────────────────────────
    symbols_to_close = _symbols_for_close(assessments)
    if symbols_to_close and not settings.DRY_RUN:
        await _cancel_pending_for_symbols(client, symbols_to_close, open_orders)

    # ── 8. Execute closes ─────────────────────────────────────────────────────
    for a in assessments:
        if not a.should_close:
            continue
        if settings.DRY_RUN:
            report.actions_taken.append({
                'symbol': a.symbol,
                'action': 'would_close',
                'dry_run': True,
                'details': a.details,
            })
            continue
        try:
            action = await execute_risk_assessment(client=client, assessment=a)
            report.actions_taken.append(action)
        except Exception as exc:
            err = f"execute({a.symbol}): {exc}"
            report.errors.append(err)
            logger.error(err)

    # ── 9. Persist ────────────────────────────────────────────────────────────
    try:
        await save_run_record(report.to_run_record())
    except Exception as exc:
        logger.warning("save_run_record failed: %s", exc)

    logger.info(
        "Run complete | run_id=%s structures=%s orphans=%d closes=%d deferred=%d errors=%d",
        report.run_id, struct_summary, len(orphans),
        report.closes_triggered(), report.closes_deferred, len(report.errors),
    )
    return report


def _log_assessment(a: RiskAssessment) -> None:
    tag = "CLOSE" if a.should_close else " HOLD"
    reason = f"reason={a.reason.value}" if a.reason else ""
    logger.info(
        "  [%s] %-12s %-15s %s | %s",
        tag, a.structure_type.value, a.symbol, reason, a.details,
    )


if __name__ == "__main__":
    import asyncio
    report = asyncio.run(run_once())
    logger.info(
        "Monitor done | positions=%d closes=%d deferred=%d errors=%d",
        report.total_positions,
        report.closes_triggered(),
        report.closes_deferred,
        len(report.errors),
    )
