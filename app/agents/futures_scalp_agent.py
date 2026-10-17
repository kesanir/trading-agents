from __future__ import annotations

from typing import Any, Dict, Optional

from app.agents.futures_agent import build_futures_bias


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _map_qqq_to_nq(qqq_price: Optional[float], nq_price: Optional[float], qqq_level: Optional[float]) -> Optional[float]:
    """
    Map a QQQ level to an NQ-equivalent level using the current ratio.
    """
    qqq = _safe_float(qqq_price, 0.0)
    nq = _safe_float(nq_price, 0.0)
    lvl = _safe_float(qqq_level, 0.0)

    if qqq <= 0 or nq <= 0 or lvl <= 0:
        return None

    ratio = nq / qqq
    return round(lvl * ratio, 2)


def _build_long_plan(
    qqq_spot: float,
    qqq_call_wall: Optional[float],
    qqq_put_wall: Optional[float],
    qqq_zero_gamma: Optional[float],
    nq_price: Optional[float],
    confidence: str,
    day_type: str,
) -> Dict[str, Any]:
    """
    Long scalp framework:
    - Buy dips if positive alignment exists.
    - Use put wall as broad invalidation.
    - Use call wall as first target.
    """
    entry_qqq = round(qqq_spot * 0.9975, 2)  # slight dip entry reference
    stop_qqq = qqq_put_wall if qqq_put_wall is not None else round(qqq_spot * 0.985, 2)
    target_1_qqq = qqq_call_wall if qqq_call_wall is not None else round(qqq_spot * 1.01, 2)

    target_2_qqq = None
    if qqq_zero_gamma is not None and qqq_call_wall is not None and qqq_zero_gamma > qqq_call_wall:
        target_2_qqq = qqq_zero_gamma

    return {
        "instrument": "MNQ",
        "direction": "LONG",
        "setup": "buy_dips",
        "day_type": day_type,
        "confidence": confidence,
        "qqq_entry_ref": entry_qqq,
        "qqq_stop": stop_qqq,
        "qqq_target_1": target_1_qqq,
        "qqq_target_2": target_2_qqq,
        "nq_entry_ref": _map_qqq_to_nq(qqq_spot, nq_price, entry_qqq),
        "nq_stop": _map_qqq_to_nq(qqq_spot, nq_price, stop_qqq),
        "nq_target_1": _map_qqq_to_nq(qqq_spot, nq_price, target_1_qqq),
        "nq_target_2": _map_qqq_to_nq(qqq_spot, nq_price, target_2_qqq) if target_2_qqq else None,
        "entry_note": "Buy pullbacks / hold above VWAP / higher-low confirmation preferred.",
        "invalidation_note": "Lose support / fail to hold bid / leaders roll over.",
    }


def _build_short_plan(
    qqq_spot: float,
    qqq_call_wall: Optional[float],
    qqq_put_wall: Optional[float],
    qqq_zero_gamma: Optional[float],
    nq_price: Optional[float],
    confidence: str,
    day_type: str,
) -> Dict[str, Any]:
    """
    Short scalp framework:
    - Sell rips if bearish alignment exists.
    - Use call wall as broad invalidation.
    - Use put wall as first target.
    """
    entry_qqq = round(qqq_spot * 1.0025, 2)  # slight pop entry reference
    stop_qqq = qqq_call_wall if qqq_call_wall is not None else round(qqq_spot * 1.015, 2)
    target_1_qqq = qqq_put_wall if qqq_put_wall is not None else round(qqq_spot * 0.99, 2)

    target_2_qqq = None
    if qqq_zero_gamma is not None and qqq_put_wall is not None and qqq_zero_gamma < qqq_put_wall:
        target_2_qqq = qqq_zero_gamma

    return {
        "instrument": "MNQ",
        "direction": "SHORT",
        "setup": "sell_rips",
        "day_type": day_type,
        "confidence": confidence,
        "qqq_entry_ref": entry_qqq,
        "qqq_stop": stop_qqq,
        "qqq_target_1": target_1_qqq,
        "qqq_target_2": target_2_qqq,
        "nq_entry_ref": _map_qqq_to_nq(qqq_spot, nq_price, entry_qqq),
        "nq_stop": _map_qqq_to_nq(qqq_spot, nq_price, stop_qqq),
        "nq_target_1": _map_qqq_to_nq(qqq_spot, nq_price, target_1_qqq),
        "nq_target_2": _map_qqq_to_nq(qqq_spot, nq_price, target_2_qqq) if target_2_qqq else None,
        "entry_note": "Sell failed pops / VWAP rejection / lower-high confirmation preferred.",
        "invalidation_note": "Reclaim resistance / upside breadth expansion / leaders squeeze higher.",
    }


def _build_neutral_plan(
    qqq_spot: float,
    qqq_call_wall: Optional[float],
    qqq_put_wall: Optional[float],
    nq_price: Optional[float],
    confidence: str,
    day_type: str,
    lean: str,
) -> Dict[str, Any]:
    """
    Neutral framework:
    - no aggressive trade
    - but provide directional lean and levels to watch
    """
    watch_above = qqq_call_wall
    watch_below = qqq_put_wall

    playbook = "wait"
    entry_note = "No trade yet. Wait for alignment or opening range confirmation."

    if lean == "BULLISH":
        playbook = "wait_for_long_confirmation"
        entry_note = "Bullish lean: watch for reclaim / hold / higher-low confirmation."
    elif lean == "BEARISH":
        playbook = "wait_for_short_confirmation"
        entry_note = "Bearish lean: watch for failed bounce / lower-high confirmation."

    return {
        "instrument": "MNQ",
        "direction": "NEUTRAL",
        "setup": playbook,
        "day_type": day_type,
        "confidence": confidence,
        "qqq_spot": round(qqq_spot, 2),
        "qqq_watch_above": watch_above,
        "qqq_watch_below": watch_below,
        "nq_watch_above": _map_qqq_to_nq(qqq_spot, nq_price, watch_above),
        "nq_watch_below": _map_qqq_to_nq(qqq_spot, nq_price, watch_below),
        "entry_note": entry_note,
        "invalidation_note": "Stand aside until structure improves.",
    }


def build_futures_scalp_plan(
    nq_price: Optional[float] = None,
    use_mag7: bool = True,
) -> Dict[str, Any]:
    """
    Build an intraday futures scalp plan based on:
    - higher-level futures bias
    - QQQ walls / spot / zero gamma
    - current NQ price (optional but recommended)
    """

    bias_obj = build_futures_bias(index_symbol="QQQ", use_mag7=use_mag7)

    bias = bias_obj.get("bias", "NEUTRAL")
    lean = bias_obj.get("lean", "NONE")
    confidence = bias_obj.get("confidence", "LOW")
    day_type = bias_obj.get("day_type", "watch")

    trade_plan = bias_obj.get("trade_plan", {})
    qqq_spot = _safe_float(trade_plan.get("qqq_spot"))
    qqq_call_wall = trade_plan.get("qqq_call_wall")
    qqq_put_wall = trade_plan.get("qqq_put_wall")
    qqq_zero_gamma = trade_plan.get("qqq_zero_gamma")

    scalp_allowed = bias in ("LONG", "SHORT")
    caution = confidence == "LOW" or day_type in ("mixed", "range")

    if bias == "LONG":
        plan = _build_long_plan(
            qqq_spot=qqq_spot,
            qqq_call_wall=qqq_call_wall,
            qqq_put_wall=qqq_put_wall,
            qqq_zero_gamma=qqq_zero_gamma,
            nq_price=nq_price,
            confidence=confidence,
            day_type=day_type,
        )
    elif bias == "SHORT":
        plan = _build_short_plan(
            qqq_spot=qqq_spot,
            qqq_call_wall=qqq_call_wall,
            qqq_put_wall=qqq_put_wall,
            qqq_zero_gamma=qqq_zero_gamma,
            nq_price=nq_price,
            confidence=confidence,
            day_type=day_type,
        )
    else:
        plan = _build_neutral_plan(
            qqq_spot=qqq_spot,
            qqq_call_wall=qqq_call_wall,
            qqq_put_wall=qqq_put_wall,
            nq_price=nq_price,
            confidence=confidence,
            day_type=day_type,
            lean=lean,
        )

    return {
        "scalp_allowed": scalp_allowed and not caution,
        "bias": bias,
        "lean": lean,
        "confidence": confidence,
        "day_type": day_type,
        "caution": caution,
        "higher_timeframe": bias_obj,
        "scalp_plan": plan,
    }