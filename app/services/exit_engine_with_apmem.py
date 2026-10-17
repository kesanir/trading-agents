from __future__ import annotations

from typing import Any, Dict, List

from alpaca.trading.client import TradingClient
from core.config import settings

from app.features.position_parser import parse_all_positions, detect_structures
from app.features.risk_evaluator import (
    evaluate_structure,
    evaluate_single_position,
)
from app.services.apmem_exit_overlay import build_apmem_exit_overlay


def evaluate_exits_with_apmem(symbol_for_apmem: str = "QQQ") -> List[Dict[str, Any]]:
    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    raw_positions = []
    for p in client.get_all_positions():
        raw_positions.append({
            "symbol": p.symbol,
            "asset_class": getattr(p, "asset_class", "option"),
            "qty": getattr(p, "qty", 0),
            "avg_entry_price": getattr(p, "avg_entry_price", 0),
            "current_price": getattr(p, "current_price", 0),
            "market_value": getattr(p, "market_value", 0),
            "cost_basis": getattr(p, "cost_basis", 0),
            "unrealized_pl": getattr(p, "unrealized_pl", 0),
            "unrealized_plpc": getattr(p, "unrealized_plpc", 0),
        })

    positions = parse_all_positions(raw_positions)
    structures, orphans = detect_structures(positions)

    overlay = build_apmem_exit_overlay(symbol=symbol_for_apmem)
    exits: List[Dict[str, Any]] = []

    # Structure-aware exits
    for s in structures:
        assessment = evaluate_structure(s)

        should_close = assessment.should_close
        reason = assessment.reason.value if assessment.reason else None
        details = assessment.details

        # Intraday APMEM overlay
        plpc = getattr(s, "unrealized_plpc", 0.0)

        # Profit lock: if in profit, take it sooner
        if overlay["profit_lock"] and plpc is not None and plpc >= 0.25:
            should_close = True
            reason = reason or "APMEM_PROFIT_LOCK"
            details = f"{details} | Intraday APMEM profit lock" if details else "Intraday APMEM profit lock"

        # Tight stop: cut losers faster
        if overlay["tight_stop"] and plpc is not None and plpc <= -0.35:
            should_close = True
            reason = reason or "APMEM_TIGHT_STOP"
            details = f"{details} | Intraday APMEM tight stop" if details else "Intraday APMEM tight stop"

        # Force defensive exit for short premium structures during active panic
        if overlay["force_defensive_exit"]:
            structure_type = type(s).__name__.lower()
            if structure_type in ["creditspread", "ironcondor"] and plpc is not None and plpc <= -0.10:
                should_close = True
                reason = reason or "APMEM_DEFENSIVE_EXIT"
                details = f"{details} | Active panic defensive exit" if details else "Active panic defensive exit"

        if should_close:
            exits.append({
                "kind": "structure",
                "symbol": assessment.symbol,
                "structure_type": assessment.structure_type.value,
                "reason": reason,
                "details": details,
                "overlay": overlay,
                "structure": s,
            })

    # Single-leg / orphan exits
    for p in orphans:
        assessment = evaluate_single_position(p)

        should_close = assessment.should_close
        reason = assessment.reason.value if assessment.reason else None
        details = assessment.details
        plpc = getattr(p, "unrealized_plpc", 0.0)

        if overlay["profit_lock"] and plpc is not None and plpc >= 0.20:
            should_close = True
            reason = reason or "APMEM_PROFIT_LOCK"
            details = f"{details} | Intraday APMEM profit lock" if details else "Intraday APMEM profit lock"

        if overlay["tight_stop"] and plpc is not None and plpc <= -0.25:
            should_close = True
            reason = reason or "APMEM_TIGHT_STOP"
            details = f"{details} | Intraday APMEM tight stop" if details else "Intraday APMEM tight stop"

        if should_close:
            exits.append({
                "kind": "orphan",
                "symbol": p.symbol,
                "reason": reason,
                "details": details,
                "overlay": overlay,
                "position": p,
            })

    return exits