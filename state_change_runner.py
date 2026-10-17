from __future__ import annotations

import sys
from datetime import datetime, timedelta,timezone
from typing import Any, Dict

import logging
import re

from app.services.market_hours_service import is_trigger_window, is_market_open_window
from app.data.alpaca_intraday import get_intraday_context
from app.agents.futures_trigger_agent import build_futures_trigger
from app.features.gex_features import get_spx_0dte_context
from app.services.state_service import load_state, save_state
from app.services.formatting_service import (
    format_futures_trigger,
    format_state_change_alert,
    format_spx_0dte_gex,
    format_risk_context,
)
from app.services.telegram_service import send_telegram_message
from app.services.persistence_service import save_report
from app.services.trade_logger import log_signal
from app.services.signal_outcome_service import resolve_pending_signals
from app.agents.risk_agent import compute_risk_context
from app.services.invalidation_service import (
    check_invalidation,
    set_active_signal,
    get_active_signal,
    format_invalidation_alert,
)
from app.services.intraday_apmem_service import build_intraday_apmem_signal
from app.services.signal_file_service import write_signal_file, build_signal_data

log = logging.getLogger("state_change_runner")

STATE_FILE = "last_trigger_state.json"
META_FILE = "last_trigger_meta.json"

DEBOUNCE_RUNS = 2
COOLDOWN_MINUTES = 10

_STATE_KEYS = [
    "bias",
    "lean",
    "confidence",
    "day_type",
    "trade_triggered",
    "trigger_type",
    "spx_0dte_regime",
    "apmem_state",
    "apmem_transition",
]


def extract_signal_state(
    trigger_obj: Dict[str, Any],
    spx_0dte_ctx: Dict[str, Any],
    intraday_apmem: Dict[str, Any],
) -> Dict[str, Any]:
    trigger = trigger_obj.get("trigger", {}) or {}
    return {
        "bias": trigger_obj.get("bias"),
        "lean": trigger_obj.get("lean"),
        "confidence": trigger_obj.get("confidence"),
        "day_type": trigger_obj.get("day_type"),
        "trade_triggered": trigger_obj.get("trade_triggered"),
        "trigger_type": trigger.get("trigger_type"),
        "spx_0dte_regime": spx_0dte_ctx.get("current", {}).get("regime"),
        "apmem_state": intraday_apmem.get("apmem_state"),
        "apmem_transition": intraday_apmem.get("apmem_transition"),
    }


def state_changed(old: Dict[str, Any], new: Dict[str, Any]) -> bool:
    if not old:
        return True
    return any(old.get(k) != new.get(k) for k in _STATE_KEYS)


def state_signature(state: Dict[str, Any]) -> str:
    return "|".join(str(state.get(k, "")) for k in _STATE_KEYS)


def cooldown_active(meta: Dict[str, Any]) -> bool:
    last_alert_ts = meta.get("last_alert_ts")
    if not last_alert_ts:
        return False
    try:
        last_dt = datetime.fromisoformat(last_alert_ts)
    except Exception:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < (last_dt + timedelta(minutes=COOLDOWN_MINUTES))


def _safe_contracts(value: Any, default: int = 1) -> int:
    try:
        v = int(value)
        return max(1, v)
    except Exception:
        return default


def main():
    args = {a.lower() for a in sys.argv[1:]}
    from app.services.deployment_safety import check_safety
    check_safety()
    from core.config import settings
    write_signals = "--write-signals" in args and "--execute" in args and "--dry-run" not in args and not settings.DRY_RUN
    force = "--force" in args
    if not is_market_open_window():
        print("Skipped: market closed (including holidays/early closes).")
        return
    send_telegram = "--telegram" in args

    # Resolve pending signal outcomes throughout the market session,
    # independent of the narrower state-change trigger window.
    if force or is_market_open_window():
        try:
            resolved = resolve_pending_signals()
            if resolved:
                print(f"Resolved {resolved} pending signal(s).")
        except Exception as e:
            log.warning("Outcome resolution failed: %s", e)

    # Grace tick: outside the trigger window we still run once more if a
    # debounce is mid-flight, so a pending state change can confirm instead
    # of being orphaned when the window closes.
    if not force and not is_trigger_window():
        meta_peek = load_state(META_FILE)
        if int(meta_peek.get("pending_count", 0)) == 0:
            print("Skipped state-change runner: outside trigger window.")
            return
        print(
            f"Outside trigger window, but debounce pending "
            f"({meta_peek.get('pending_count')}/{DEBOUNCE_RUNS}) — running grace tick."
        )

    # ── Market context ────────────────────────────────────────────────────────
    market = get_intraday_context("QQQ", opening_range_minutes=30)

    # ── Intraday APMEM ───────────────────────────────────────────────────────
    intraday_apmem_obj = build_intraday_apmem_signal(symbol="QQQ")
    intraday_apmem = intraday_apmem_obj.to_dict()

    apmem_state = intraday_apmem_obj.apmem_state
    apmem_prev = intraday_apmem_obj.prev_apmem_state
    apmem_transition = intraday_apmem_obj.apmem_transition
    apmem_size_mult = intraday_apmem_obj.apmem_size_mult

    # ── Futures trigger ──────────────────────────────────────────────────────
    trigger_obj = build_futures_trigger(
        current_price=market["price"],
        vwap=market["vwap"],
        or_high=market["or_high"],
        or_low=market["or_low"],
        nq_price=market["price"],
        use_mag7=True,
    )

    # ── SPX 0DTE GEX (sourced from SPY — Alpaca doesn't support cash indices) ─
    spx_0dte_ctx = get_spx_0dte_context("SPY")
    spx_cur = spx_0dte_ctx.get("current", {})
    spx_0dte_error = spx_0dte_ctx.get("error")

    # ── Risk context (compute before APMEM gating so we can scale it) ───────
    risk_ctx = compute_risk_context(trigger_obj)

    # ── Intraday APMEM gating / sizing overlay ──────────────────────────────
    apmem_gate_note = "No intraday APMEM override"

    # Hard block: active / entering panic
    if apmem_state == "PANIC" and apmem_prev in ["VOL_EXPANSION", "PANIC"]:
        trigger_obj["trade_triggered"] = False
        apmem_gate_note = "Blocked: active panic regime"

    # Late panic: possible bottoming / flush zone, allow but cut size
    elif apmem_state == "PANIC" and apmem_prev == "STABILIZING":
        current_contracts = _safe_contracts(risk_ctx.get("position_size_contracts"), 1)
        risk_ctx["position_size_contracts"] = max(1, int(current_contracts * 0.5))
        apmem_gate_note = "Late panic: reduced size to 50%"

    # Expansion: allow but reduce size
    elif apmem_state == "VOL_EXPANSION":
        current_contracts = _safe_contracts(risk_ctx.get("position_size_contracts"), 1)
        risk_ctx["position_size_contracts"] = max(1, int(current_contracts * 0.7))
        apmem_gate_note = "Vol expansion: reduced size to 70%"

    # Strong reversal zone
    elif apmem_transition == "PANIC_TO_RECOVERY":
        current_contracts = _safe_contracts(risk_ctx.get("position_size_contracts"), 1)
        risk_ctx["position_size_contracts"] = max(1, int(current_contracts * 1.2))
        apmem_gate_note = "Panic to recovery: boosted size by 20%"

    # Generic size overlay
    else:
        current_contracts = _safe_contracts(risk_ctx.get("position_size_contracts"), 1)
        risk_ctx["position_size_contracts"] = max(1, int(current_contracts * float(apmem_size_mult)))
        apmem_gate_note = f"Applied intraday APMEM size multiplier: {apmem_size_mult}"

    # ── Signal logging ───────────────────────────────────────────────────────
    exec_plan = trigger_obj.get("execution_plan") or {}
    trigger_detail = trigger_obj.get("trigger") or {}
    mag7_ctx = trigger_obj.get("mag7") or {}
    apmem_ctx = trigger_obj.get("apmem") or {}

    now_utc = datetime.now(timezone.utc)
    time_bucket = f"{now_utc.hour:02d}:00"

    signal_row = {
        # identity
        "symbol": "MNQ",
        "timestamp": now_utc.isoformat(),
        "date": now_utc.date().isoformat(),
        "hour_of_day": now_utc.hour,
        "minute_of_hour": now_utc.minute,
        "time_bucket": time_bucket,

        # core trade state
        "bias": trigger_obj.get("bias"),
        "lean": trigger_obj.get("lean"),
        "confidence": trigger_obj.get("confidence"),
        "day_type": trigger_obj.get("day_type"),
        "trade_triggered": trigger_obj.get("trade_triggered"),
        "trigger_type": trigger_detail.get("trigger_type"),
        "setup": exec_plan.get("setup"),

        # execution plan
        "entry_ref": exec_plan.get("entry_ref"),
        "stop": exec_plan.get("stop"),
        "target_1": exec_plan.get("target_1"),
        "target_2": exec_plan.get("target_2"),
        "size_mult": exec_plan.get("size_mult"),

        # intraday market context
        "qqq_price": market.get("price"),
        "qqq_vwap": market.get("vwap"),
        "qqq_or_high": market.get("or_high"),
        "qqq_or_low": market.get("or_low"),

        # MAG7 breadth
        "mag7_dominant_regime": mag7_ctx.get("dominant_regime"),
        "mag7_broad_confirmation": mag7_ctx.get("broad_confirmation"),
        "mag7_top_pick": mag7_ctx.get("top_pick"),

        # SPX 0DTE GEX
        "spx_0dte_underlying": spx_cur.get("ticker"),
        "spx_0dte_regime": spx_cur.get("regime"),
        "spx_0dte_net_gex_b": spx_cur.get("net_gex_b"),
        "spx_0dte_zero_gamma": spx_cur.get("zero_gamma"),
        "spx_0dte_call_wall": spx_cur.get("call_wall"),
        "spx_0dte_put_wall": spx_cur.get("put_wall"),
        "spx_0dte_regime_flipped": spx_0dte_ctx.get("regime_flipped", False),
        "spx_0dte_zero_gamma_crossed": spx_0dte_ctx.get("zero_gamma_crossed", False),

        # Intraday APMEM
        "apmem_state": apmem_state,
        "apmem_prev_state": apmem_prev,
        "apmem_transition": apmem_transition,
        "apmem_vol": intraday_apmem_obj.apmem_vol,
        "apmem_trend": intraday_apmem_obj.apmem_trend,
        "apmem_accel": intraday_apmem_obj.apmem_accel,
        "apmem_bias": apmem_ctx.get("bias"),
        "apmem_size_mult": apmem_ctx.get("size_mult"),
        "apmem_blocked": apmem_ctx.get("blocked", False),
        "apmem_block_reason": apmem_ctx.get("block_reason"),

        # optional metadata for later segmentation
        "market_alignment": trigger_obj.get("market_alignment"),
        "opening_scenario": trigger_obj.get("opening_scenario"),
        "playbook": trigger_obj.get("playbook"),
    }
    
    signal_id = log_signal(signal_row)
    print(f"Signal logged: {signal_id}")

    # Add risk fields after compute_risk_context
    signal_row["risk_grade"] = risk_ctx.get("risk_grade")
    signal_row["risk_reward_ratio"] = risk_ctx.get("risk_reward_ratio")
    signal_row["position_size_contracts"] = risk_ctx.get("position_size_contracts")

    # ── Invalidation check ───────────────────────────────────────────────────
    invalidation_reasons = check_invalidation(
        current_price=market.get("price", 0),
        vwap=market.get("vwap", 0),
        spx_0dte_ctx=spx_0dte_ctx,
        trigger_obj=trigger_obj,
    )

    previous_invalidation = load_state("deployment_invalidation.json")
    invalidation_signature = [re.sub(r"\d+(?:\.\d+)?", "#", reason) for reason in invalidation_reasons]
    if invalidation_reasons:
        active = get_active_signal()
        inv_msg = format_invalidation_alert(invalidation_reasons, active or signal_row)
        print(inv_msg)
        if send_telegram and previous_invalidation.get("signature") != invalidation_signature:
            try:
                send_telegram_message(inv_msg, parse_mode=None)
            except Exception as e:
                log.warning("Telegram send failed (invalidation alert): %s", e)
                raise
        save_state("deployment_invalidation.json", {"signature": invalidation_signature})
    elif previous_invalidation:
        save_state("deployment_invalidation.json", {})

    # Track active signal when live trigger fires
    if trigger_obj.get("trade_triggered"):
        set_active_signal({**signal_row, "signal_id": signal_id})

    # ── Format message blocks ────────────────────────────────────────────────
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    full_trigger = format_futures_trigger(trigger_obj)
    spx_gex_msg = format_spx_0dte_gex(spx_0dte_ctx)
    risk_msg = format_risk_context(risk_ctx)
    header_warnings = f"WARNING: 0DTE GEX unavailable ({spx_0dte_error})\n\n" if spx_0dte_error else ""

    apmem_msg = (
        "=== INTRADAY APMEM ===\n"
        f"State: {apmem_state}\n"
        f"Prev: {apmem_prev}\n"
        f"Transition: {apmem_transition}\n"
        f"Signal: {intraday_apmem_obj.apmem_signal}\n"
        f"Bias: {intraday_apmem_obj.apmem_bias}\n"
        f"Vol: {intraday_apmem_obj.apmem_vol}\n"
        f"Trend: {intraday_apmem_obj.apmem_trend}\n"
        f"Accel: {intraday_apmem_obj.apmem_accel}\n"
        f"Size Mult: {intraday_apmem_obj.apmem_size_mult}\n"
        f"Gate Note: {apmem_gate_note}"
    )

    # ── State change detection ───────────────────────────────────────────────
    new_state = extract_signal_state(trigger_obj, spx_0dte_ctx, intraday_apmem)
    old_state = load_state(STATE_FILE)
    meta = load_state(META_FILE)

    new_sig = state_signature(new_state)
    pending_sig = meta.get("pending_sig")
    pending_count = int(meta.get("pending_count", 0))

    changed = state_changed(old_state, new_state)

    if not changed:
        meta["pending_sig"] = None
        meta["pending_count"] = 0
        save_state(META_FILE, meta)

        status_msg = (
            f"##### TRIGGER STATUS | {timestamp} #####\n"
            f"{header_warnings}"
            f"Signal ID: {signal_id}\n\n"
            f"{apmem_msg}\n\n"
            f"{risk_msg}\n\n"
            f"{spx_gex_msg}\n\n"
            f"{full_trigger}"
        )

        print(status_msg)

        return

    # ── Debounce ─────────────────────────────────────────────────────────────
    if pending_sig == new_sig:
        pending_count += 1
    else:
        pending_sig = new_sig
        pending_count = 1

    meta["pending_sig"] = pending_sig
    meta["pending_count"] = pending_count
    save_state(META_FILE, meta)

    if pending_count < DEBOUNCE_RUNS:
        print(f"State changed, but waiting for debounce confirmation ({pending_count}/{DEBOUNCE_RUNS}).")
        return

    if cooldown_active(meta):
        print("State changed and debounced, but cooldown is active. No alert sent.")
        return

    # ── Format and send state change alert ───────────────────────────────────
    change_msg = format_state_change_alert(old_state, new_state)

    full_msg = (
        f"##### STATE CHANGE | {timestamp} #####\n"
        f"{header_warnings}"
        f"Signal ID: {signal_id}\n\n"
        f"{change_msg}\n\n"
        f"{apmem_msg}\n\n"
        f"{risk_msg}\n\n"
        f"{spx_gex_msg}\n\n"
        f"{full_trigger}"
    )

    print(full_msg)

    if write_signals:
        # Write NinjaTrader signal file (today's session)
        try:
            sig_data = build_signal_data(trigger_obj, intraday_apmem_obj, market)
            contracts = _safe_contracts(risk_ctx.get("position_size_contracts"), 1)
            sig_path = write_signal_file(
                instrument="MNQ",
                signal_data=sig_data,
                contracts=contracts,
            )
            print(f"NT signal file written -> {sig_path}")
        except Exception as e:
            log.warning("NT signal file write failed: %s", e)

    paths = save_report(
        report_type="state_change",
        raw={
            **trigger_obj,
            "spx_0dte": spx_0dte_ctx,
            "intraday_apmem": intraday_apmem,
            "risk_ctx": risk_ctx,
            "signal_id": signal_id,
        },
        formatted=full_msg,
    )
    print(f"Saved report → {paths['txt_path']}")

    if send_telegram:
        try:
            send_telegram_message(full_msg, parse_mode=None)
        except Exception as e:
            log.warning("Telegram send failed (state change alert): %s", e)
            raise

    save_state(STATE_FILE, new_state)

    meta["last_alert_ts"] = datetime.now(timezone.utc).isoformat()
    meta["pending_sig"] = None
    meta["pending_count"] = 0
    save_state(META_FILE, meta)


if __name__ == "__main__":
    main()
