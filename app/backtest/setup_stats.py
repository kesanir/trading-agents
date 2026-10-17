"""
setup_stats.py — Per-setup edge analysis across signal outcomes.

Answers: "Which setups have actual edge, and under what conditions?"

Breakdowns:
  - Per trigger_type (ORB long vs VWAP hold vs ORB short, etc.)
  - By regime alignment (QQQ + MAG7 both agree vs mixed)
  - By SPX 0DTE regime (positive vs negative gamma)
  - By confidence level

Usage:
  python -c "from app.backtest.setup_stats import build_setup_stats; import pprint; pprint.pprint(build_setup_stats())"
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from collections import defaultdict
from typing import Any, Dict, List, Tuple

from app.services.trade_logger import read_signal_outcomes
from app.services.signal_quality_service import summarize_outcomes, group_outcomes


def _regime_alignment(outcome: Dict[str, Any]) -> str:
    """
    Classify regime alignment at the time of the signal.
    Reads mag7_dominant_regime and bias from the outcome (carried from signal_row).
    """
    bias = (outcome.get("bias") or "").upper()
    mag7 = (outcome.get("mag7_dominant_regime") or "").lower()

    if not bias or not mag7:
        return "unknown"

    bias_dir = "positive" if bias == "LONG" else "negative" if bias == "SHORT" else "neutral"

    if bias_dir == mag7:
        return "aligned"
    elif mag7 == "mixed":
        return "mixed"
    else:
        return "divergent"


def build_setup_stats(outcomes: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Compute edge statistics broken down by all relevant setup dimensions.

    Returns:
        by_trigger_type       — accuracy + MFE/MAE per trigger type
        by_regime_alignment   — aligned vs divergent vs mixed
        by_spx_0dte_regime    — positive gamma vs negative gamma
        by_confidence         — HIGH / MEDIUM / LOW
        by_bias               — LONG / SHORT
        by_day_type           — trend / watch / range / mixed
        cross_tab_summary     — top 5 best and worst (trigger_type, day_type) combos
    """
    if outcomes is None:
        outcomes = read_signal_outcomes()

    if not outcomes:
        return {"error": "No resolved signal outcomes. Run state_change_runner.py to collect data."}

    # Enrich outcomes with derived alignment field
    enriched = []
    for o in outcomes:
        enriched.append({**o, "_alignment": _regime_alignment(o)})

    # Standard groupings
    by_trigger   = group_outcomes(enriched, "trigger_type")
    by_alignment = group_outcomes(enriched, "_alignment")
    by_spx       = group_outcomes(enriched, "spx_0dte_regime")
    by_conf      = group_outcomes(enriched, "confidence")
    by_bias      = group_outcomes(enriched, "bias")
    by_day       = group_outcomes(enriched, "day_type")

    # Cross-tab: (trigger_type, day_type) pairs — find best/worst combos
    cross: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for o in enriched:
        key = (str(o.get("trigger_type", "?")), str(o.get("day_type", "?")))
        cross[key].append(o)

    cross_summaries = []
    for (ttype, dtype), rows in cross.items():
        if len(rows) < 3:  # need at least 3 samples for meaningful stats
            continue
        s = summarize_outcomes(rows)
        acc = s.get("accuracy_30m")
        cross_summaries.append({
            "trigger_type": ttype,
            "day_type": dtype,
            "signals": len(rows),
            "accuracy_30m": acc,
            "avg_mfe": s.get("avg_mfe"),
            "stop_hit_rate": s.get("stop_hit_rate"),
        })

    cross_summaries.sort(key=lambda x: x.get("accuracy_30m") or 0, reverse=True)

    return {
        "total_outcomes": len(outcomes),
        "by_trigger_type":     by_trigger,
        "by_regime_alignment": by_alignment,
        "by_spx_0dte_regime":  by_spx,
        "by_confidence":       by_conf,
        "by_bias":             by_bias,
        "by_day_type":         by_day,
        "best_combos":  cross_summaries[:5],
        "worst_combos": cross_summaries[-5:] if len(cross_summaries) >= 5 else [],
    }
