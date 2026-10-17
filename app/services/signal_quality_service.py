"""
signal_quality_service.py — Signal accuracy and quality analytics.

Reads signal_outcomes.jsonl and produces directional accuracy breakdowns by
bias, confidence, day_type, trigger_type, hour_of_day, and SPX 0DTE regime.
Follows the same group_summary() pattern as performance_service.py.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.services.trade_logger import read_signal_outcomes


def _accuracy_for(outcomes: List[Dict[str, Any]], horizon: str = "30m") -> Dict[str, Any]:
    """
    Summarise directional accuracy for a list of outcomes at the given horizon.
    horizon: "15m" | "30m" | "60m"
    """
    correct_key = f"correct_at_{horizon}"
    values = [o.get(correct_key) for o in outcomes if o.get(correct_key) is not None]

    if not values:
        return {
            "signals": len(outcomes),
            "resolved": 0,
            f"accuracy_{horizon}": None,
        }

    correct = sum(1 for v in values if v is True)
    return {
        "signals": len(outcomes),
        "resolved": len(values),
        f"accuracy_{horizon}": round(correct / len(values), 4) if values else None,
    }


def _mfe_mae_stats(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    mfes = [float(o["mfe"]) for o in outcomes if o.get("mfe") is not None]
    maes = [float(o["mae"]) for o in outcomes if o.get("mae") is not None]
    hit_stop = [o for o in outcomes if o.get("hit_stop")]
    hit_target = [o for o in outcomes if o.get("hit_target_1")]

    def _avg(lst: List[float]) -> Optional[float]:
        return round(sum(lst) / len(lst), 4) if lst else None

    total = len(outcomes)
    return {
        "avg_mfe": _avg(mfes),
        "avg_mae": _avg(maes),
        "stop_hit_rate": round(len(hit_stop) / total, 4) if total else None,
        "target_hit_rate": round(len(hit_target) / total, 4) if total else None,
    }


def summarize_outcomes(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Full accuracy + excursion summary for a list of outcome records."""
    if not outcomes:
        return {"signals": 0, "resolved": 0}

    result = {
        "signals": len(outcomes),
        **_accuracy_for(outcomes, "15m"),
        **_accuracy_for(outcomes, "30m"),
        **_accuracy_for(outcomes, "60m"),
        **_mfe_mae_stats(outcomes),
    }
    # Remove duplicate "signals" key from sub-calls
    result["signals"] = len(outcomes)
    return result


def group_outcomes(
    outcomes: List[Dict[str, Any]], field: str
) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in outcomes:
        key = str(o.get(field, "UNKNOWN"))
        groups[key].append(o)
    return {k: summarize_outcomes(v) for k, v in groups.items()}


def build_signal_quality_report() -> Dict[str, Any]:
    outcomes = read_signal_outcomes()
    return {
        "overall": summarize_outcomes(outcomes),
        "by_bias": group_outcomes(outcomes, "bias"),
        "by_confidence": group_outcomes(outcomes, "confidence"),
        "by_day_type": group_outcomes(outcomes, "day_type"),
        "by_trigger_type": group_outcomes(outcomes, "trigger_type"),
        "by_hour": group_outcomes(outcomes, "hour_of_day"),
        "by_spx_0dte_regime": group_outcomes(outcomes, "spx_0dte_regime"),
    }
