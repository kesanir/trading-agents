"""
replay_engine.py — Signal replay and virtual execution simulation.

Reads signal_outcomes.jsonl and simulates whether a trade placed at entry_ref
would have hit target_1 or stop first within the recorded MFE/MAE excursions.

Output format is compatible with performance_service.summarize_trades() so
results can be formatted by the same formatter.

Usage:
  python -c "from app.backtest.replay_engine import replay_signals; import pprint; pprint.pprint(replay_signals())"
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from typing import Any, Dict, List, Optional

from app.services.trade_logger import read_signal_outcomes


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def simulate_outcome(outcome: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Given a resolved signal outcome, simulate virtual trade execution.

    Assumptions:
    - Entry fills at entry_ref (limit order, conservative)
    - Stop and target are hit based on MFE/MAE recorded in the outcome
    - A trade is 'filled' if price moved at least 1 tick (0.25 pts) favorably
    - 'won' if hit_target_1 without hit_stop first
    - 'lost' if hit_stop (even if target also hit — stop is prioritised on loss)

    Returns a trade-like dict compatible with summarize_trades(), or None if
    the signal cannot be simulated (missing data).
    """
    entry_ref = _safe_float(outcome.get("entry_ref"))
    stop      = outcome.get("stop")
    target    = outcome.get("target_1")

    if not entry_ref or stop is None or target is None:
        return None

    stop_f   = _safe_float(stop)
    target_f = _safe_float(target)

    if stop_f == 0 or target_f == 0:
        return None

    stop_dist   = abs(entry_ref - stop_f)
    target_dist = abs(entry_ref - target_f)
    if stop_dist == 0:
        return None

    hit_stop   = bool(outcome.get("hit_stop"))
    hit_target = bool(outcome.get("hit_target_1"))
    mfe        = _safe_float(outcome.get("mfe"))
    mae        = _safe_float(outcome.get("mae"))

    # Conservative: entry fills only if price moved favorably at all
    entry_fill = mfe > 0.25

    if not entry_fill:
        return None  # Never triggered — skip

    # Determine outcome: stop takes priority if both hit (worst-case)
    if hit_stop and hit_target:
        # Which was hit first? We don't have bar-level data in outcomes,
        # so assume the adverse move (MAE) came after favorable (conservative).
        # If MAE >= stop_dist: loss. Else: win.
        won = mae < stop_dist
    elif hit_target:
        won = True
    elif hit_stop:
        won = False
    else:
        # Neither hit — use direction at 60m as proxy
        correct_60m = outcome.get("correct_at_60m")
        if correct_60m is None:
            return None
        won = bool(correct_60m)

    pnl = target_dist if won else -stop_dist
    rr  = round(target_dist / stop_dist, 2) if stop_dist > 0 else None

    return {
        "timestamp":    outcome.get("signal_ts", outcome.get("resolved_at", "")),
        "signal_id":    outcome.get("signal_id"),
        "bias":         outcome.get("bias"),
        "confidence":   outcome.get("confidence"),
        "day_type":     outcome.get("day_type"),
        "trigger_type": outcome.get("trigger_type"),
        "setup":        outcome.get("setup"),
        "entry_ref":    entry_ref,
        "stop":         stop_f,
        "target_1":     target_f,
        "pnl":          round(pnl, 4),
        "result":       "WIN" if won else "LOSS",
        "r_multiple":   rr if won else -1.0,
        "mfe":          mfe,
        "mae":          mae,
        "simulated":    True,
    }


def replay_signals(outcomes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Replay all resolved signal outcomes through the virtual execution model.

    Args:
        outcomes: Pre-loaded outcomes list (for testing). Reads from JSONL if None.

    Returns dict with:
        simulated_trades  — list of virtual trade records
        fill_rate         — fraction of signals that would have triggered
        summary           — summarize_trades()-compatible stats dict
        by_trigger_type   — stats grouped by trigger_type
        by_confidence     — stats grouped by confidence
    """
    from app.services.performance_service import summarize_trades, group_summary

    if outcomes is None:
        outcomes = read_signal_outcomes()

    if not outcomes:
        return {
            "simulated_trades": [],
            "fill_rate": 0.0,
            "total_outcomes": 0,
            "simulated_count": 0,
            "skipped": 0,
            "summary": summarize_trades([]),
            "by_trigger_type": {},
            "by_confidence": {},
            "by_bias": {},
        }

    total = len(outcomes)
    trades: List[Dict[str, Any]] = []
    skipped = 0

    for o in outcomes:
        result = simulate_outcome(o)
        if result is None:
            skipped += 1
        else:
            trades.append(result)

    fill_rate = round(len(trades) / total, 4) if total else 0.0

    return {
        "simulated_trades": trades,
        "fill_rate": fill_rate,
        "total_outcomes": total,
        "simulated_count": len(trades),
        "skipped": skipped,
        "summary": summarize_trades(trades),
        "by_trigger_type": group_summary(trades, "trigger_type"),
        "by_confidence": group_summary(trades, "confidence"),
        "by_bias": group_summary(trades, "bias"),
    }
