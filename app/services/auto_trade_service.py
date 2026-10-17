from __future__ import annotations

from typing import Dict, Any, Optional


def pick_trade_candidate(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    top3 = plan.get("mag7_top3_setups", [])
    if not top3:
        return None

    best = top3[0]

    score = float(best.get("score", 0.0))
    regime = str(best.get("regime", "")).lower()
    size_mult = float(best.get("size_mult", 1.0))

    global_regime = plan.get("global_regime", {}) or {}
    apmem_state = str(global_regime.get("apmem_state", "NEUTRAL")).upper()
    prev_state = str(global_regime.get("prev_apmem_state", "UNKNOWN")).upper()
    apmem_size_mult = float(global_regime.get("apmem_size_mult", 1.0))
    regime_score = int(global_regime.get("score", 50))
    bias = str(global_regime.get("bias", "WAIT")).upper()

    futures_bias = str(plan.get("futures_bias", "NEUTRAL")).upper()
    futures_lean = str(plan.get("futures_lean", "NEUTRAL")).upper()
    broad_confirmation = bool(plan.get("mag7_broad_confirmation", False))
    dominant_regime = str(plan.get("mag7_dominant_regime", "mixed")).lower()

    # Base filters
    if 45 <= regime_score <= 55:
        return None

    if bias == "WAIT":
        return None

    if score < 0.55:
        return None

    if size_mult < 0.70:
        return None

    # Direction / strategy mapping
    # Strong alignment: futures_bias is directional and matches Mag7 regime
    # Soft alignment: futures_bias=NEUTRAL but futures_lean matches Mag7 regime
    #                 (requires broad_confirmation and dominant_regime match; size cut)
    alignment = None
    if regime == "positive" and futures_bias == "LONG":
        direction = "bullish"
        strategy = "credit_put"
        alignment = "strong"
    elif regime == "negative" and futures_bias == "SHORT":
        direction = "bearish"
        strategy = "credit_call"
        alignment = "strong"
    elif (
        futures_bias == "NEUTRAL"
        and broad_confirmation
        and dominant_regime == regime
    ):
        if regime == "positive" and futures_lean == "BULLISH":
            direction = "bullish"
            strategy = "credit_put"
            alignment = "soft"
        elif regime == "negative" and futures_lean == "BEARISH":
            direction = "bearish"
            strategy = "credit_call"
            alignment = "soft"
        else:
            return None
    else:
        return None

    if alignment == "soft":
        size_mult *= 0.70

    # -------------------------------------------------
    # APMEM state overlay
    # -------------------------------------------------

    # Always apply model size multiplier
    size_mult *= apmem_size_mult

    # Hard block only in worst case
    #if apmem_state == "PANIC":
        # Allow only strongest aligned setups, very small size
    #    if score < 0.70 or not broad_confirmation or dominant_regime != regime:
    #        return None
    #    size_mult *= 0.40
    
    # strongest signal
    if apmem_state == "RECOVERY" and prev_state == "PANIC":
        size_mult *= 1.3  
    # Entering panic (worst zone)
    if apmem_state == "PANIC" and prev_state == "VOL_EXPANSION":
        return None
    # Deep panic (avoid)
    if prev_state == "PANIC":
        return None
    if apmem_state == "PANIC" and prev_state == "STABILIZING":
        if score < 0.65 or not broad_confirmation:
            return None
        size_mult *= 0.5

    elif apmem_state == "VOL_EXPANSION":
        # More selective, smaller size
        if score < 0.62:
            return None
        size_mult *= 0.70

    elif apmem_state == "STABILIZING":
        # Slightly cautious but tradable
        size_mult *= 0.90

    elif apmem_state == "RECOVERY":
        # Best long-risk-on environment
        size_mult *= 1.10

    elif apmem_state == "CALM":
        # Normal sizing
        size_mult *= 1.00

    else:
        # NEUTRAL / unknown
        size_mult *= 0.90

    # Final floor / cap
    size_mult = max(0.40, min(1.50, size_mult))

    return {
        "ticker": str(best["ticker"]).upper(),
        "direction": direction,
        "strategy": strategy,
        "dte": 7,
        "delta": 0.35,
        "width": 5,
        "score": round(score, 4),
        "size_mult": round(size_mult, 2),
        "apmem_state": apmem_state,
        "regime_score": regime_score,
        "alignment": alignment,
    }