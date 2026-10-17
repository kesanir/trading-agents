from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from app.agents.regime_agent import classify_regime
from app.agents.mag7_agent import build_mag7_summary
from app.agents.futures_agent import build_futures_bias
from app.data.vix_data import get_vix_data


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


def _infer_atm_iv_from_candidate(row: Dict[str, Any]) -> Optional[float]:
    iv_analytics = row.get("iv_analytics", {}) or {}
    atm_iv = iv_analytics.get("atm_iv")
    if atm_iv is None:
        atm_iv = iv_analytics.get("current_iv")
    return _clean_num(atm_iv, 4)


def _compute_expected_move(spot: Optional[float], atm_iv: Optional[float], days: int = 1) -> Dict[str, Optional[float]]:
    if spot is None or atm_iv is None:
        return {
            "expected_move": None,
            "expected_upper": None,
            "expected_lower": None,
        }

    try:
        move = float(spot) * float(atm_iv) * math.sqrt(days / 365.0)
        return {
            "expected_move": round(move, 2),
            "expected_upper": round(float(spot) + move, 2),
            "expected_lower": round(float(spot) - move, 2),
        }
    except Exception:
        return {
            "expected_move": None,
            "expected_upper": None,
            "expected_lower": None,
        }


def _build_market_condition(
    qqq_regime: str,
    mag7_regime: str,
    broad_confirmation: bool,
    vol_regime: str,
) -> str:
    if qqq_regime == "negative" and vol_regime == "high_vol":
        return "trend_down_risk"
    if qqq_regime == "positive" and vol_regime == "low_vol":
        return "pin_or_grind_up"
    if qqq_regime == mag7_regime and broad_confirmation:
        if qqq_regime == "positive":
            return "aligned_bullish"
        if qqq_regime == "negative":
            return "aligned_bearish"
    if qqq_regime != mag7_regime:
        return "mixed_cross_current"
    return "watchful_range"


def _build_opening_scenario(
    qqq_spot: Optional[float],
    qqq_call_wall: Optional[float],
    qqq_put_wall: Optional[float],
) -> str:
    if qqq_spot is None:
        return "unknown"

    if qqq_call_wall is not None and qqq_spot > qqq_call_wall:
        return "above_call_wall_breakout_risk"
    if qqq_put_wall is not None and qqq_spot < qqq_put_wall:
        return "below_put_wall_breakdown_risk"

    if qqq_call_wall is not None and qqq_put_wall is not None:
        return "inside_wall_range"

    return "inside_structure_watch"


def _build_playbook(
    bias: str,
    lean: str,
    vol_regime: str,
    market_condition: str,
) -> str:
    if bias == "LONG":
        if vol_regime == "low_vol":
            return "buy_dips_but_expect_grind_not_expansion"
        return "buy_dips_and_breakout_continuations"

    if bias == "SHORT":
        if vol_regime == "high_vol":
            return "sell_rips_and_momentum_breakdowns"
        return "sell_rips_with_tighter_profit_taking"

    if lean == "BULLISH":
        if market_condition == "mixed_cross_current":
            return "wait_for_reclaim_then_selective_longs"
        return "bullish_lean_wait_for_confirmation"

    if lean == "BEARISH":
        if market_condition == "mixed_cross_current":
            return "wait_for_failure_then_selective_shorts"
        return "bearish_lean_wait_for_confirmation"

    return "no_trade_until_structure_is_clear"


def build_premarket_plan(index_symbol: str = "QQQ") -> Dict[str, Any]:
    qqq = classify_regime(index_symbol)
    mag7 = build_mag7_summary()
    futures = build_futures_bias(index_symbol=index_symbol, use_mag7=True)
    vix_data = get_vix_data()

    qqq_regime = qqq.get("regime", "")
    qqq_score = _safe_float(qqq.get("score"))
    qqq_strength = _safe_float(qqq.get("strength"))
    qqq_call_wall = _clean_num(qqq.get("call_wall"), 2)
    qqq_put_wall = _clean_num(qqq.get("put_wall"), 2)
    qqq_zero_gamma = _clean_num(qqq.get("zero_gamma"), 2)
    qqq_net_gex_b = _clean_num(qqq.get("net_gex_b"), 4)
    qqq_spot = _clean_num(qqq.get("spot"), 2)

    dominant_regime = mag7.get("dominant_regime", "mixed")
    broad_confirmation = bool(mag7.get("broad_confirmation", False))
    leaders = mag7.get("leaders", [])
    laggards = mag7.get("laggards", [])
    top_pick = mag7.get("top_pick")
    mag7_raw = mag7.get("raw", []) or []

    bias = futures.get("bias", "NEUTRAL")
    lean = futures.get("lean", "NONE")
    confidence = futures.get("confidence", "LOW")
    day_type = futures.get("day_type", "watch")

    # use top ranked Mag7 candidate ATM IV as proxy for current market expected move
    proxy_iv = None
    if mag7_raw:
        proxy_iv = _infer_atm_iv_from_candidate(mag7_raw[0])

    expected = _compute_expected_move(qqq_spot, proxy_iv, days=1)

    vix = vix_data.get("vix")
    vix_change = vix_data.get("vix_change")
    vol_regime = vix_data.get("vol_regime", "unknown")

    market_condition = _build_market_condition(
        qqq_regime=qqq_regime,
        mag7_regime=dominant_regime,
        broad_confirmation=broad_confirmation,
        vol_regime=vol_regime,
    )

    opening_scenario = _build_opening_scenario(
        qqq_spot=qqq_spot,
        qqq_call_wall=qqq_call_wall,
        qqq_put_wall=qqq_put_wall,
    )

    playbook = _build_playbook(
        bias=bias,
        lean=lean,
        vol_regime=vol_regime,
        market_condition=market_condition,
    )

    if bias == "LONG":
        opening_plan = "Prefer buy-the-dip setups above VWAP / support."
    elif bias == "SHORT":
        opening_plan = "Prefer sell-the-rip setups into resistance / failed reclaims."
    elif lean == "BULLISH":
        opening_plan = "Mixed tape with bullish lean. Wait for reclaim / opening strength before longs."
    elif lean == "BEARISH":
        opening_plan = "Mixed tape with bearish lean. Wait for failed bounce / opening weakness before shorts."
    else:
        opening_plan = "No strong edge premarket. Stand aside until structure is clearer."

    if broad_confirmation and qqq_regime != dominant_regime:
        risk_note = "Index and leadership disagree. Avoid forcing directional trades."
    elif not broad_confirmation:
        risk_note = "Breadth is weak. Expect more chop / lower conviction."
    else:
        risk_note = "Structure is aligned enough to matter. Use intraday confirmation."

    notes: List[str] = []
    notes.append(f"QQQ spot={qqq_spot}")
    notes.append(f"QQQ regime={qqq_regime}")
    notes.append(f"QQQ net_gex_b={qqq_net_gex_b}")
    notes.append(f"QQQ walls call={qqq_call_wall} put={qqq_put_wall}")
    if qqq_zero_gamma is not None:
        notes.append(f"QQQ zero_gamma={qqq_zero_gamma}")
    notes.append(f"VIX={vix} change={vix_change} vol_regime={vol_regime}")
    notes.append(f"Mag7 dominant regime={dominant_regime}")
    notes.append(f"Broad confirmation={broad_confirmation}")
    if leaders:
        notes.append(f"Leaders={', '.join(leaders[:3])}")
    if laggards:
        notes.append(f"Laggards={', '.join(laggards[:3])}")
    if expected["expected_move"] is not None:
        notes.append(
            f"Expected move={expected['expected_move']} range={expected['expected_lower']} - {expected['expected_upper']}"
        )

    return {
        "index_symbol": index_symbol,
        "qqq_spot": qqq_spot,
        "qqq_regime": qqq_regime,
        "qqq_score": round(qqq_score, 4),
        "qqq_strength": round(qqq_strength, 4),
        "qqq_net_gex_b": qqq_net_gex_b,
        "qqq_call_wall": qqq_call_wall,
        "qqq_put_wall": qqq_put_wall,
        "qqq_zero_gamma": qqq_zero_gamma,
        "vix": vix,
        "vix_change": vix_change,
        "vol_regime": vol_regime,
        "proxy_atm_iv": proxy_iv,
        "expected_move": expected["expected_move"],
        "expected_upper": expected["expected_upper"],
        "expected_lower": expected["expected_lower"],
        "mag7_dominant_regime": dominant_regime,
        "mag7_broad_confirmation": broad_confirmation,
        "mag7_leaders": leaders,
        "mag7_laggards": laggards,
        "mag7_top_pick": top_pick,
        "futures_bias": bias,
        "futures_lean": lean,
        "confidence": confidence,
        "day_type": day_type,
        "market_condition": market_condition,
        "opening_scenario": opening_scenario,
        "playbook": playbook,
        "opening_plan": opening_plan,
        "risk_note": risk_note,
        "notes": notes,
    }