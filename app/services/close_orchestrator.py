"""
services/close_orchestrator.py  (extended)
Safe close sequencing: CreditSpread, IronCondor, CalendarSpread, single.
Short legs always bought back BEFORE long legs are sold.
"""
from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide

from core.config import settings
from core.models import (
    AssetClass, CalendarSpread, CloseReason, CreditSpread,
    IronCondor, Position, RiskAssessment,
)
from core.logging import get_logger
from app.data.alpaca_trading import close_equity_position, submit_option_order

logger = get_logger(__name__)


async def _buy_leg(client: TradingClient, pos: Position, label: str) -> dict:
    contracts = int(abs(pos.qty))
    logger.info("  [BUY]  %-20s %s x%d", label, pos.symbol, contracts)
    if settings.DRY_RUN:
        return {"leg": label, "order_id": "dry_run", "symbol": pos.symbol}
    order = await submit_option_order(client, pos.symbol, contracts, OrderSide.BUY)
    return {"leg": label, "order_id": str(order.get("id")), "symbol": pos.symbol}


async def _sell_leg(client: TradingClient, pos: Position, label: str) -> dict:
    contracts = int(abs(pos.qty))
    logger.info("  [SELL] %-20s %s x%d", label, pos.symbol, contracts)
    if settings.DRY_RUN:
        return {"leg": label, "order_id": "dry_run", "symbol": pos.symbol}
    order = await submit_option_order(client, pos.symbol, contracts, OrderSide.SELL)
    return {"leg": label, "order_id": str(order.get("id")), "symbol": pos.symbol}


async def close_spread(client: TradingClient, spread: CreditSpread, reason: CloseReason) -> dict:
    result = {"underlying": spread.underlying, "structure": spread.spread_type,
              "reason": reason.value, "dry_run": settings.DRY_RUN, "orders": []}
    logger.info("[SPREAD CLOSE] %s | %s | %s", spread.underlying, spread.spread_type, reason.value)
    try:
        result["orders"].append(await _buy_leg(client, spread.short_leg.position, "short_buyback"))
    except Exception as exc:
        msg = f"short_buyback failed: {exc}"
        logger.error("CRITICAL: %s", msg)
        if settings.ABORT_ON_SHORT_BUYBACK_FAILURE:
            result["error"] = msg + " — ABORT to avoid naked position"
            return result
        logger.warning("ABORT_ON_SHORT_BUYBACK_FAILURE=false — proceeding to sell long leg")
    try:
        result["orders"].append(await _sell_leg(client, spread.long_leg.position, "long_sell"))
    except Exception as exc:
        result["warning"] = f"long_sell failed: {exc}"
        logger.warning(result["warning"])
    return result


async def close_iron_condor(client: TradingClient, condor: IronCondor, reason: CloseReason) -> dict:
    result = {"underlying": condor.underlying, "structure": "iron_condor",
              "reason": reason.value, "dry_run": settings.DRY_RUN, "orders": []}
    logger.info("[CONDOR CLOSE] %s | %s", condor.underlying, reason.value)

    put_short  = condor.put_spread.short_leg.position
    call_short = condor.call_spread.short_leg.position
    put_long   = condor.put_spread.long_leg.position
    call_long  = condor.call_spread.long_leg.position

    # Phase 1: both short legs must succeed
    short_errors = []
    for pos, label in [(put_short, "put_short_buyback"), (call_short, "call_short_buyback")]:
        try:
            result["orders"].append(await _buy_leg(client, pos, label))
        except Exception as exc:
            msg = f"{label} failed: {exc}"
            logger.error("CRITICAL: %s", msg)
            short_errors.append(msg)

    if short_errors:
        result["error"] = "; ".join(short_errors)
        result["warning"] = "Short leg buybacks incomplete — long legs NOT sold to preserve hedge"
        return result

    # Phase 2: sell long legs
    for pos, label in [(put_long, "put_long_sell"), (call_long, "call_long_sell")]:
        try:
            result["orders"].append(await _sell_leg(client, pos, label))
        except Exception as exc:
            result.setdefault("warnings", []).append(f"{label} failed: {exc}")
            logger.warning("WARNING: %s %s failed: %s", label, pos.symbol, exc)

    return result


async def close_calendar(client: TradingClient, cal: CalendarSpread, reason: CloseReason) -> dict:
    result = {"underlying": cal.underlying, "structure": cal.spread_type,
              "reason": reason.value, "dry_run": settings.DRY_RUN, "orders": []}
    logger.info("[CALENDAR CLOSE] %s | %s | %s", cal.underlying, cal.spread_type, reason.value)
    try:
        result["orders"].append(await _buy_leg(client, cal.short_leg.position, "near_short_buyback"))
    except Exception as exc:
        result["error"] = f"near_short_buyback failed: {exc} — ABORT"
        logger.error("CRITICAL: %s", result["error"])
        return result
    try:
        result["orders"].append(await _sell_leg(client, cal.long_leg.position, "far_long_sell"))
    except Exception as exc:
        result["warning"] = f"far_long_sell failed: {exc}"
        logger.warning(result["warning"])
    return result


async def close_single_position(client: TradingClient, position: Position, reason: CloseReason) -> dict:
    result = {"symbol": position.symbol, "asset_class": position.asset_class.value,
              "side": position.side.value, "reason": reason.value, "dry_run": settings.DRY_RUN}
    logger.info("[POSITION CLOSE] %s | %s | %s", position.symbol, position.side.value, reason.value)
    if settings.DRY_RUN:
        result["order_id"] = "dry_run"
        return result
    if position.asset_class == AssetClass.EQUITY:
        order = await close_equity_position(client=client, symbol=position.symbol)
        result["order_id"] = str(order.get("id"))
    elif position.asset_class == AssetClass.OPTION:
        side = OrderSide.BUY if position.side.value == "short" else OrderSide.SELL
        order = await submit_option_order(client, position.symbol, int(abs(position.qty)), side)
        result["order_id"] = str(order.get("id"))
    else:
        result["error"] = f"unsupported_asset_class: {position.asset_class}"
    return result


async def execute_risk_assessment(client: TradingClient, assessment: RiskAssessment) -> dict:
    if not assessment.should_close:
        return {"symbol": assessment.symbol, "action": "hold", "details": assessment.details}

    structure = assessment.structure
    reason    = assessment.reason

    if isinstance(structure, IronCondor):
        return await close_iron_condor(client, structure, reason)
    if isinstance(structure, CreditSpread):
        return await close_spread(client, structure, reason)
    if isinstance(structure, CalendarSpread):
        return await close_calendar(client, structure, reason)
    if assessment.orphan_position is not None:
        return await close_single_position(client, assessment.orphan_position, reason)

    logger.error("No closeable entity for %s", assessment.symbol)
    return {"symbol": assessment.symbol, "error": "no_closeable_entity"}
