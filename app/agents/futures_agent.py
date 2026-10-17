from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.agents.regime_agent import classify_regime
from app.agents.mag7_agent import build_mag7_summary


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_num(value: Any, digits: int = 4) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _count_regimes(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    pos = sum(1 for r in rows if r.get("regime") == "positive")
    neg = sum(1 for r in rows if r.get("regime") == "negative")
    return {"positive": pos, "negative": neg}


def _top_by_regime(rows: List[Dict[str, Any]], regime: str, n: int = 3) -> List[str]:
    return [r["ticker"] for r in rows if r.get("regime") == regime][:n]


def _derive_day_type(
    index_regime: str,
    index_strength: float,
    broad_confirmation: bool,
    dominant_regime: str,
) -> str:
    if index_regime in ("positive", "negative") and broad_confirmation and dominant_regime == index_regime:
        if index_strength >= 0.20:
            return "trend"
        return "watch"

    if index_regime in ("positive", "negative") and dominant_regime != index_regime:
        return "mixed"

    if not broad_confirmation:
        return "range"

    return "watch"


def _build_trade_plan(
    qqq: Dict[str, Any],
    bias: str,
    lean: str,
    confidence: str,
    day_type: str,
    reasons: List[str],
    mag7_summary: Dict[str, Any],
) -> Dict[str, Any]:
    spot = _clean_num(qqq.get("spot"), 2)
    call_wall = _clean_num(qqq.get("call_wall"), 2)
    put_wall = _clean_num(qqq.get("put_wall"), 2)
    zero_gamma = _clean_num(qqq.get("zero_gamma"), 2)
    net_gex_b = _clean_num(qqq.get("net_gex_b"), 4)
    dist_to_flip = _clean_num(qqq.get("dist_to_flip"), 3)

    target_1 = None
    target_2 = None
    invalidation = None
    playbook = "no_trade"
    entry_note = "Wait for cleaner confirmation."

    if bias == "LONG":
        playbook = "buy_dips"
        target_1 = call_wall
        target_2 = zero_gamma if zero_gamma and call_wall and zero_gamma > call_wall else None
        invalidation = put_wall
        entry_note = "Prefer pullbacks that hold above support / VWAP. Avoid chasing extended candles."

    elif bias == "SHORT":
        playbook = "sell_rips"
        target_1 = put_wall
        target_2 = zero_gamma if zero_gamma and put_wall and zero_gamma < put_wall else None
        invalidation = call_wall
        entry_note = "Prefer failed pops into resistance / VWAP rejection. Avoid pressing late breakdowns."

    else:
        if lean == "BULLISH":
            playbook = "wait_for_long_confirmation"
            entry_note = "Mixed tape with bullish lean. Wait for VWAP reclaim / opening range strength."
        elif lean == "BEARISH":
            playbook = "wait_for_short_confirmation"
            entry_note = "Mixed tape with bearish lean. Wait for failed bounce / opening range weakness."
        else:
            playbook = "no_trade_or_small_size"
            entry_note = "Mixed or unclear confirmation. Reduce size or stand aside."

    return {
        "instrument": "MNQ",
        "bias": bias,
        "lean": lean,
        "confidence": confidence,
        "day_type": day_type,
        "playbook": playbook,
        "qqq_spot": spot,
        "qqq_call_wall": call_wall,
        "qqq_put_wall": put_wall,
        "qqq_zero_gamma": zero_gamma,
        "qqq_net_gex_b": net_gex_b,
        "qqq_dist_to_flip_pct": dist_to_flip,
        "target_1": target_1,
        "target_2": target_2,
        "invalidation": invalidation,
        "entry_note": entry_note,
        "mag7_top_positive": mag7_summary.get("leaders", [])[:3],
        "mag7_top_negative": mag7_summary.get("laggards", [])[:3],
        "reasons": reasons,
    }


def build_futures_bias(
    index_symbol: str = "QQQ",
    use_mag7: bool = True,
) -> Dict[str, Any]:
    """
    Futures bias engine for NQ / MNQ.

    Logic hierarchy:
    1. Index regime (QQQ)
    2. Mag7 dominant regime + breadth confirmation
    3. Mixed-regime handling via lean
    4. Trade gating based on alignment + strength
    """

    qqq = classify_regime(index_symbol)
    qqq_regime = qqq.get("regime", "")
    qqq_score = _safe_float(qqq.get("score"))
    qqq_strength = _safe_float(qqq.get("strength"))
    qqq_call_wall = _clean_num(qqq.get("call_wall"), 2)
    qqq_put_wall = _clean_num(qqq.get("put_wall"), 2)

    reasons: List[str] = []
    bias = "NEUTRAL"
    lean = "NONE"
    confidence = "LOW"

    mag7_summary: Dict[str, Any] = {
        "leaders": [],
        "laggards": [],
        "dominant_regime": "mixed",
        "broad_confirmation": False,
        "top_pick": None,
        "raw": [],
    }

    if use_mag7:
        mag7_summary = build_mag7_summary()
        rows = mag7_summary.get("raw", [])
        counts = _count_regimes(rows)
        pos_count = counts["positive"]
        neg_count = counts["negative"]
        dominant_regime = mag7_summary.get("dominant_regime", "mixed")
        broad_confirmation = bool(mag7_summary.get("broad_confirmation"))

        reasons.append(f"QQQ regime is {qqq_regime or 'unclear'}")
        reasons.append(f"Mag7 dominant regime is {dominant_regime}")
        reasons.append(f"Mag7 broad confirmation is {broad_confirmation}")
        reasons.append(
            f"Mag7 positive={pos_count}, negative={neg_count}, top_pick={mag7_summary.get('top_pick')}"
        )

        # Full bullish alignment
        if (
            qqq_regime == "positive"
            and dominant_regime == "positive"
            and broad_confirmation
        ):
            bias = "LONG"
            lean = "BULLISH"

            if qqq_strength >= 0.20 or pos_count >= 5:
                confidence = "HIGH"
            else:
                confidence = "MEDIUM"

            reasons.append("Index and Mag7 are aligned bullish")

        # Full bearish alignment
        elif (
            qqq_regime == "negative"
            and dominant_regime == "negative"
            and broad_confirmation
        ):
            bias = "SHORT"
            lean = "BEARISH"

            if qqq_strength >= 0.20 or neg_count >= 5:
                confidence = "HIGH"
            else:
                confidence = "MEDIUM"

            reasons.append("Index and Mag7 are aligned bearish")

        # Mixed case: QQQ negative, Mag7 positive
        elif qqq_regime == "negative" and dominant_regime == "positive":
            bias = "NEUTRAL"
            lean = "BULLISH"

            # weak negative index + strong positive breadth = bullish lean
            if qqq_strength < 0.15 and pos_count >= 5:
                confidence = "LOW"
                reasons.append("QQQ is weakly negative, but Mag7 breadth is strongly positive")
            else:
                confidence = "LOW"
                reasons.append("Mixed tape: bearish index vs bullish leadership")

        # Mixed case: QQQ positive, Mag7 negative
        elif qqq_regime == "positive" and dominant_regime == "negative":
            bias = "NEUTRAL"
            lean = "BEARISH"

            if qqq_strength < 0.15 and neg_count >= 5:
                confidence = "LOW"
                reasons.append("QQQ is weakly positive, but Mag7 breadth is strongly negative")
            else:
                confidence = "LOW"
                reasons.append("Mixed tape: bullish index vs bearish leadership")

        # Index-only positive tendency, but weak breadth
        elif qqq_regime == "positive":
            bias = "NEUTRAL"
            lean = "BULLISH" if pos_count >= neg_count else "NONE"
            confidence = "LOW"
            reasons.append("QQQ positive, but Mag7 breadth is not strong enough for a full long bias")

        # Index-only negative tendency, but weak breadth
        elif qqq_regime == "negative":
            bias = "NEUTRAL"
            lean = "BEARISH" if neg_count >= pos_count else "NONE"
            confidence = "LOW"
            reasons.append("QQQ negative, but Mag7 breadth is not strong enough for a full short bias")

        else:
            bias = "NEUTRAL"
            lean = "NONE"
            confidence = "LOW"
            reasons.append("Both index and breadth are unclear")

    else:
        if qqq_regime == "positive":
            bias = "LONG" if qqq_strength >= 0.20 else "NEUTRAL"
            lean = "BULLISH"
            confidence = "MEDIUM" if qqq_strength >= 0.20 else "LOW"
            reasons.append("Index-only mode: QQQ positive regime")

        elif qqq_regime == "negative":
            bias = "SHORT" if qqq_strength >= 0.20 else "NEUTRAL"
            lean = "BEARISH"
            confidence = "MEDIUM" if qqq_strength >= 0.20 else "LOW"
            reasons.append("Index-only mode: QQQ negative regime")

        else:
            bias = "NEUTRAL"
            lean = "NONE"
            confidence = "LOW"
            reasons.append("Index-only mode: QQQ unclear")

    day_type = _derive_day_type(
        index_regime=qqq_regime,
        index_strength=qqq_strength,
        broad_confirmation=bool(mag7_summary.get("broad_confirmation")),
        dominant_regime=mag7_summary.get("dominant_regime", "mixed"),
    )

    if qqq_call_wall is not None and qqq_put_wall is not None:
        reasons.append(f"QQQ walls: call={qqq_call_wall}, put={qqq_put_wall}")

    reasons.append(f"QQQ strength={round(qqq_strength, 4)} score={round(qqq_score, 4)}")

    trade_allowed = bias in ("LONG", "SHORT")

    trade_plan = _build_trade_plan(
        qqq=qqq,
        bias=bias,
        lean=lean,
        confidence=confidence,
        day_type=day_type,
        reasons=reasons,
        mag7_summary=mag7_summary,
    )

    return {
        "index_symbol": index_symbol,
        "index_regime": qqq_regime,
        "index_score": round(qqq_score, 4),
        "index_strength": round(qqq_strength, 4),
        "bias": bias,
        "lean": lean,
        "confidence": confidence,
        "day_type": day_type,
        "trade_allowed": trade_allowed,
        "reasons": reasons,
        "mag7_summary": {
            "dominant_regime": mag7_summary.get("dominant_regime"),
            "broad_confirmation": mag7_summary.get("broad_confirmation"),
            "leaders": mag7_summary.get("leaders", []),
            "laggards": mag7_summary.get("laggards", []),
            "top_pick": mag7_summary.get("top_pick"),
        },
        "trade_plan": trade_plan,
    }