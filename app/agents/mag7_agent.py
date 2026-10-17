from __future__ import annotations

from typing import Any, Dict, List

from app.agents.regime_agent import scan_named_universe


def build_mag7_summary(top_n: int = 7) -> Dict[str, Any]:
    scan = scan_named_universe("mag7", top_n=top_n)
    candidates: List[Dict[str, Any]] = scan.get("candidates", [])

    leaders = [c["ticker"] for c in candidates if str(c.get("regime", "")).lower() == "positive"]
    laggards = [c["ticker"] for c in candidates if str(c.get("regime", "")).lower() == "negative"]

    top_pick = candidates[0]["ticker"] if candidates else None

    pos_count = sum(1 for c in candidates if str(c.get("regime", "")).lower() == "positive")
    neg_count = sum(1 for c in candidates if str(c.get("regime", "")).lower() == "negative")

    if pos_count > neg_count:
        dominant_regime = "positive"
    elif neg_count > pos_count:
        dominant_regime = "negative"
    else:
        dominant_regime = "mixed"

    broad_confirmation = pos_count >= 4 or neg_count >= 4

    return {
        "leaders": leaders,
        "laggards": laggards,
        "dominant_regime": dominant_regime,
        "broad_confirmation": broad_confirmation,
        "top_pick": top_pick,
        "raw": candidates,
        "global_regime": scan.get("global_regime"),
        "total": scan.get("total", 0),
        "loaded": scan.get("loaded", 0),
        "errors": scan.get("errors", []),
    }