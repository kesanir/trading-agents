from __future__ import annotations

from typing import Any, Dict, Optional

from app.agents.futures_scalp_agent import build_futures_scalp_plan


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_num(value: Any, digits: int = 2) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _long_trigger(
    current_price: float,
    vwap: Optional[float],
    or_high: Optional[float],
    or_low: Optional[float],
    last_pullback_low: Optional[float],
) -> Dict[str, Any]:
    """
    Long trigger logic:
    - price above VWAP
    - reclaim or hold above OR high, OR
    - hold higher low above VWAP
    """
    reasons = []
    triggered = False
    trigger_type = None

    if vwap is not None and current_price > vwap:
        reasons.append("Price is above VWAP")

        if or_high is not None and current_price > or_high:
            triggered = True
            trigger_type = "opening_range_breakout_long"
            reasons.append("Price is above OR high")

        elif last_pullback_low is not None and last_pullback_low > vwap:
            triggered = True
            trigger_type = "vwap_hold_higher_low_long"
            reasons.append("Higher low held above VWAP")

    return {
        "triggered": triggered,
        "trigger_type": trigger_type,
        "reasons": reasons,
    }


def _short_trigger(
    current_price: float,
    vwap: Optional[float],
    or_high: Optional[float],
    or_low: Optional[float],
    last_bounce_high: Optional[float],
) -> Dict[str, Any]:
    """
    Short trigger logic:
    - price below VWAP
    - break below OR low, OR
    - failed bounce below VWAP
    """
    reasons = []
    triggered = False
    trigger_type = None

    if vwap is not None and current_price < vwap:
        reasons.append("Price is below VWAP")

        if or_low is not None and current_price < or_low:
            triggered = True
            trigger_type = "opening_range_breakdown_short"
            reasons.append("Price is below OR low")

        elif last_bounce_high is not None and last_bounce_high < vwap:
            triggered = True
            trigger_type = "failed_vwap_reclaim_short"
            reasons.append("Bounce failed below VWAP")

    return {
        "triggered": triggered,
        "trigger_type": trigger_type,
        "reasons": reasons,
    }


def _neutral_trigger(
    lean: str,
    current_price: float,
    vwap: Optional[float],
    or_high: Optional[float],
    or_low: Optional[float],
) -> Dict[str, Any]:
    """
    Neutral environment:
    - no aggressive trigger
    - allow watchlist conditions only
    """
    reasons = ["Higher timeframe is neutral"]

    if lean == "BULLISH":
        reasons.append("Bullish lean active")
        if vwap is not None and current_price > vwap:
            reasons.append("Price is above VWAP")
        if or_high is not None and current_price > or_high:
            reasons.append("Price is above OR high")
        return {
            "triggered": False,
            "trigger_type": "watch_long_confirmation",
            "reasons": reasons,
        }

    if lean == "BEARISH":
        reasons.append("Bearish lean active")
        if vwap is not None and current_price < vwap:
            reasons.append("Price is below VWAP")
        if or_low is not None and current_price < or_low:
            reasons.append("Price is below OR low")
        return {
            "triggered": False,
            "trigger_type": "watch_short_confirmation",
            "reasons": reasons,
        }

    return {
        "triggered": False,
        "trigger_type": "no_trade",
        "reasons": reasons,
    }


def build_futures_trigger(
    current_price: float,
    vwap: Optional[float] = None,
    or_high: Optional[float] = None,
    or_low: Optional[float] = None,
    last_pullback_low: Optional[float] = None,
    last_bounce_high: Optional[float] = None,
    nq_price: Optional[float] = None,
    use_mag7: bool = True,
) -> Dict[str, Any]:
    """
    Final futures trigger agent.

    Inputs:
    - current_price: current NQ / MNQ price
    - vwap: session VWAP
    - or_high / or_low: opening range levels
    - last_pullback_low: for long continuation validation
    - last_bounce_high: for short continuation validation
    - nq_price: optional reference used by lower layers
    """

    scalp_obj = build_futures_scalp_plan(nq_price=nq_price or current_price, use_mag7=use_mag7)

    bias = scalp_obj.get("bias", "NEUTRAL")
    lean = scalp_obj.get("lean", "NONE")
    confidence = scalp_obj.get("confidence", "LOW")
    day_type = scalp_obj.get("day_type", "mixed")
    caution = bool(scalp_obj.get("caution", True))
    scalp_allowed = bool(scalp_obj.get("scalp_allowed", False))
    scalp_plan = scalp_obj.get("scalp_plan", {})

    reasons = [
        f"Higher timeframe bias={bias}",
        f"Lean={lean}",
        f"Confidence={confidence}",
        f"Day type={day_type}",
    ]

    if caution:
        reasons.append("Caution flag is active")

    if bias == "LONG":
        trigger = _long_trigger(
            current_price=current_price,
            vwap=vwap,
            or_high=or_high,
            or_low=or_low,
            last_pullback_low=last_pullback_low,
        )

    elif bias == "SHORT":
        trigger = _short_trigger(
            current_price=current_price,
            vwap=vwap,
            or_high=or_high,
            or_low=or_low,
            last_bounce_high=last_bounce_high,
        )

    else:
        trigger = _neutral_trigger(
            lean=lean,
            current_price=current_price,
            vwap=vwap,
            or_high=or_high,
            or_low=or_low,
        )

    trade_triggered = bool(trigger["triggered"]) and scalp_allowed and not caution

    execution_plan = {
        "instrument": scalp_plan.get("instrument", "MNQ"),
        "direction": bias if bias in ("LONG", "SHORT") else lean,
        "current_price": _clean_num(current_price, 2),
        "vwap": _clean_num(vwap, 2),
        "or_high": _clean_num(or_high, 2),
        "or_low": _clean_num(or_low, 2),
        "entry_ref": scalp_plan.get("nq_entry_ref"),
        "stop": scalp_plan.get("nq_stop"),
        "target_1": scalp_plan.get("nq_target_1"),
        "target_2": scalp_plan.get("nq_target_2"),
        "setup": trigger.get("trigger_type"),
        "entry_note": scalp_plan.get("entry_note"),
        "invalidation_note": scalp_plan.get("invalidation_note"),
    }

    return {
        "trade_triggered": trade_triggered,
        "bias": bias,
        "lean": lean,
        "confidence": confidence,
        "day_type": day_type,
        "scalp_allowed": scalp_allowed,
        "caution": caution,
        "trigger": trigger,
        "reasons": reasons + trigger.get("reasons", []),
        "execution_plan": execution_plan,
        "higher_timeframe": scalp_obj,
    }