from __future__ import annotations

from typing import Any, Dict

from app.services.intraday_apmem_service import build_intraday_apmem_signal


def build_apmem_exit_overlay(symbol: str = "QQQ") -> Dict[str, Any]:
    sig = build_intraday_apmem_signal(symbol=symbol, period="5d", interval="5m")

    state = sig.apmem_state
    prev_state = sig.prev_apmem_state
    transition = sig.apmem_transition

    overlay = {
        "state": state,
        "prev_state": prev_state,
        "transition": transition,
        "profit_lock": False,
        "tight_stop": False,
        "force_defensive_exit": False,
        "notes": [],
    }

    # Hard defensive zone
    if state == "PANIC" and prev_state in ["VOL_EXPANSION", "PANIC"]:
        overlay["tight_stop"] = True
        overlay["force_defensive_exit"] = True
        overlay["notes"].append("Active panic regime")

    # Late flush / unstable
    elif state == "PANIC" and prev_state == "STABILIZING":
        overlay["tight_stop"] = True
        overlay["notes"].append("Late panic after stabilizing")

    # Expansion still building
    elif state == "VOL_EXPANSION":
        overlay["tight_stop"] = True
        overlay["notes"].append("Volatility expansion")

    # Best reversal zone: lock gains faster
    elif transition == "PANIC_TO_RECOVERY":
        overlay["profit_lock"] = True
        overlay["notes"].append("Panic to recovery transition")

    elif state == "RECOVERY":
        overlay["profit_lock"] = True
        overlay["notes"].append("Recovery regime")

    elif state == "CALM":
        overlay["notes"].append("Calm regime")

    return overlay