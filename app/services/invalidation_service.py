"""
invalidation_service.py — Exit / invalidation signal detection.

Called from state_change_runner on each 30-min tick. Given the last active
signal (persisted in app/state/active_signal.json), checks whether any of
the following invalidation conditions are met:

  1. Price broke the stop level
  2. SPX 0DTE regime flipped vs. signal entry regime
  3. Day type degraded to range/mixed (from trend at entry)
  4. VWAP lost (longs: price < vwap) or reclaimed (shorts: price > vwap)

If any condition fires, returns a list of reason strings for the alert.

Usage (from state_change_runner):
  from app.services.invalidation_service import check_invalidation, set_active_signal, clear_active_signal
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.services.state_service import load_state, save_state

_ACTIVE_SIGNAL_FILE = "active_signal.json"


# ── Active signal persistence ─────────────────────────────────────────────────

def set_active_signal(signal_row: Dict[str, Any]) -> None:
    """Persist the current trigger signal as the active position reference."""
    save_state(_ACTIVE_SIGNAL_FILE, {
        **signal_row,
        "activated_at": datetime.now().isoformat(),
    })


def get_active_signal() -> Optional[Dict[str, Any]]:
    state = load_state(_ACTIVE_SIGNAL_FILE)
    return state if state else None


def clear_active_signal() -> None:
    """Call when a trade is manually closed or invalidated."""
    save_state(_ACTIVE_SIGNAL_FILE, {})


# ── Invalidation checks ───────────────────────────────────────────────────────

def check_invalidation(
    current_price: float,
    vwap: float,
    spx_0dte_ctx: Dict[str, Any],
    trigger_obj: Dict[str, Any],
) -> List[str]:
    """
    Check all invalidation conditions against the active signal.

    Returns a list of human-readable reason strings.
    Empty list means no invalidation.
    """
    active = get_active_signal()
    if not active:
        return []

    bias      = active.get("bias", "")
    stop      = active.get("stop")
    entry_spx = active.get("spx_0dte_regime")
    entry_day = active.get("day_type", "")

    reasons: List[str] = []

    # 1. Stop level breach
    if stop is not None:
        try:
            stop_f = float(stop)
            if bias == "LONG" and current_price < stop_f:
                reasons.append(
                    f"Price {current_price:.2f} broke stop {stop_f:.2f} (LONG invalidated)"
                )
            elif bias == "SHORT" and current_price > stop_f:
                reasons.append(
                    f"Price {current_price:.2f} broke stop {stop_f:.2f} (SHORT invalidated)"
                )
        except (TypeError, ValueError):
            pass

    # 2. SPX 0DTE regime flip
    curr_spx_regime = spx_0dte_ctx.get("current", {}).get("regime")
    regime_flipped  = spx_0dte_ctx.get("regime_flipped", False)
    if regime_flipped and entry_spx and curr_spx_regime and entry_spx != curr_spx_regime:
        reasons.append(
            f"SPX 0DTE regime flipped {entry_spx} → {curr_spx_regime}"
        )

    # 3. Day type degraded (was trend at entry, now choppy)
    curr_day_type = trigger_obj.get("day_type", "")
    if entry_day == "trend" and curr_day_type in ("range", "mixed", "watch"):
        reasons.append(
            f"Day type degraded: {entry_day} → {curr_day_type}"
        )

    # 4. VWAP lost / reclaimed
    if vwap and current_price:
        if bias == "LONG" and current_price < vwap:
            reasons.append(
                f"VWAP lost (price {current_price:.2f} < vwap {vwap:.2f}) — long support broken"
            )
        elif bias == "SHORT" and current_price > vwap:
            reasons.append(
                f"VWAP reclaimed (price {current_price:.2f} > vwap {vwap:.2f}) — short invalidated"
            )

    return reasons


def format_invalidation_alert(reasons: List[str], active_signal: Dict[str, Any]) -> str:
    """Format an exit alert message from invalidation reasons."""
    bias   = active_signal.get("bias", "?")
    setup  = active_signal.get("setup", "?")
    ts     = active_signal.get("activated_at", "?")
    sig_id = active_signal.get("signal_id", "?")

    lines = [
        f"🚨 EXIT SIGNAL | {datetime.now().strftime('%H:%M:%S')}",
        f"Signal: {sig_id}  |  {bias} {setup}  (entered ~{ts[:16]})",
        "",
        "Invalidation reasons:",
    ]
    for r in reasons:
        lines.append(f"  ⚠  {r}")
    lines.append("")
    lines.append("Action: Consider closing or tightening stop.")
    return "\n".join(lines)
