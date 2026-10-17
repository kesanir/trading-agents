"""
signal_outcome_service.py — Automated forward-price resolution for logged signals.

Called on each 30-min cron tick from state_change_runner.py. For every signal
in signals.jsonl that is older than min_age_minutes and not yet resolved, it:
  1. Fetches QQQ 1-min bars via alpaca_intraday.fetch_intraday_bars()
  2. Extracts price at signal_time + 15m, +30m, +60m
  3. Computes directional accuracy, MFE, MAE, stop hit, target hit
  4. Writes the result to signal_outcomes.jsonl

Outcome records are keyed by signal_id; each signal is resolved at most once.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services.trade_logger import (
    read_signals,
    read_signal_outcomes,
    log_signal_outcome,
)
from app.data.alpaca_intraday import fetch_intraday_bars

log = logging.getLogger("signal_outcome_service")

# How old must a signal be before we try to resolve it?
_MIN_AGE_MINUTES = 15
# Lookback window to give room for the +60m horizon
_FETCH_LOOKBACK = 120


def _parse_ts(ts_str: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            # trade_logger writes naive local time; attach local tz before converting
            dt = dt.astimezone()
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _price_at_offset(bars_df: Any, signal_dt: datetime, offset_minutes: int) -> Optional[float]:
    """Return close price from the bar closest to signal_dt + offset_minutes."""
    target = signal_dt + timedelta(minutes=offset_minutes)
    if bars_df is None or bars_df.empty:
        return None

    import pandas as pd  # local import — already a project dep

    # Make timestamp column timezone-aware if needed
    ts_col = bars_df["timestamp"]
    if hasattr(ts_col.dtype, "tz") and ts_col.dtype.tz is None:
        ts_col = ts_col.dt.tz_localize("UTC")
    elif not hasattr(ts_col.dtype, "tz"):
        ts_col = pd.to_datetime(ts_col, utc=True)

    diffs = (ts_col - target).abs()
    idx = diffs.idxmin()
    diff_secs = diffs[idx].total_seconds()
    if diff_secs > 300:  # more than 5 min off — don't use
        return None
    return float(bars_df.loc[idx, "close"])


def _compute_excursions(
    bars_df: Any,
    signal_dt: datetime,
    entry_ref: float,
    stop: Optional[float],
    target_1: Optional[float],
    bias: str,
    horizon_minutes: int = 60,
) -> Dict[str, Any]:
    """
    Walk bars from signal_dt to signal_dt + horizon_minutes.
    Compute MFE (max favorable excursion) and MAE (max adverse excursion)
    relative to entry_ref, and whether stop/target were hit.
    """
    if bars_df is None or bars_df.empty or entry_ref <= 0:
        return {}

    import pandas as pd

    ts_col = bars_df["timestamp"]
    try:
        if hasattr(ts_col.dtype, "tz") and ts_col.dtype.tz is None:
            bars_df = bars_df.copy()
            bars_df["timestamp"] = ts_col.dt.tz_localize("UTC")
        else:
            bars_df = bars_df.copy()
            bars_df["timestamp"] = pd.to_datetime(ts_col, utc=True)
    except Exception:
        return {}

    window = bars_df[
        (bars_df["timestamp"] >= signal_dt) &
        (bars_df["timestamp"] <= signal_dt + timedelta(minutes=horizon_minutes))
    ]
    if window.empty:
        return {}

    long = bias == "LONG"
    excursions = []
    adverse = []
    hit_stop = False
    hit_target = False

    for _, row in window.iterrows():
        high = float(row.get("high", 0) or 0)
        low = float(row.get("low", 0) or 0)
        favorable = (high - entry_ref) if long else (entry_ref - low)
        adv = (entry_ref - low) if long else (high - entry_ref)
        excursions.append(favorable)
        adverse.append(adv)

        if stop is not None:
            if (long and low <= stop) or (not long and high >= stop):
                hit_stop = True
        if target_1 is not None:
            if (long and high >= target_1) or (not long and low <= target_1):
                hit_target = True

    mfe = max(excursions) if excursions else 0.0
    mae = max(adverse) if adverse else 0.0

    return {
        "mfe": round(mfe, 4),
        "mae": round(mae, 4),
        "hit_stop": hit_stop,
        "hit_target_1": hit_target,
    }


def resolve_pending_signals(
    min_age_minutes: int = _MIN_AGE_MINUTES,
    symbol: str = "QQQ",
) -> int:
    """
    Resolve all unresolved signals older than min_age_minutes.
    Returns the count of newly resolved signals.
    """
    signals = read_signals()
    if not signals:
        return 0

    # Build set of already-resolved signal_ids
    outcomes = read_signal_outcomes()
    resolved_ids = {o.get("signal_id") for o in outcomes if o.get("signal_id")}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=min_age_minutes)

    pending = [
        s for s in signals
        if s.get("signal_id") not in resolved_ids
        and s.get("trade_triggered")  # only resolve active trigger signals
        and _parse_ts(s.get("timestamp", "")) is not None
        and _parse_ts(s["timestamp"]) <= cutoff
    ]

    if not pending:
        log.info("No pending signals to resolve.")
        return 0

    # Fetch bars once — lookback must reach the oldest pending signal + 60m horizon
    oldest_dt = min(_parse_ts(s["timestamp"]) for s in pending)
    age_minutes = int((now - oldest_dt).total_seconds() // 60)
    lookback = max(_FETCH_LOOKBACK, age_minutes + 90)
    try:
        bars_df = fetch_intraday_bars(symbol=symbol, lookback_minutes=lookback)
    except Exception as e:
        log.warning("Failed to fetch bars for outcome resolution: %s", e)
        return 0

    resolved = 0
    for sig in pending:
        signal_id = sig.get("signal_id", "")
        signal_dt = _parse_ts(sig["timestamp"])

        bias = sig.get("bias", "")
        entry_ref = float(sig.get("entry_ref") or 0.0)
        stop = sig.get("stop")
        target_1 = sig.get("target_1")

        p15 = _price_at_offset(bars_df, signal_dt, 15)
        p30 = _price_at_offset(bars_df, signal_dt, 30)
        p60 = _price_at_offset(bars_df, signal_dt, 60)

        # Directional correctness — did price move with bias?
        def _correct(price: Optional[float]) -> Optional[bool]:
            if price is None or entry_ref <= 0:
                return None
            return (price > entry_ref) if bias == "LONG" else (price < entry_ref)

        excursions = _compute_excursions(
            bars_df, signal_dt, entry_ref,
            stop=float(stop) if stop else None,
            target_1=float(target_1) if target_1 else None,
            bias=bias,
        )

        if p15 is None and p30 is None and p60 is None and not excursions:
            log.info("Skipping signal %s — no bar coverage yet, will retry", signal_id)
            continue

        outcome: Dict[str, Any] = {
            "signal_id":          signal_id,
            "signal_ts":          sig.get("timestamp"),
            "symbol":             symbol,
            "bias":               bias,
            "confidence":         sig.get("confidence"),
            "day_type":           sig.get("day_type"),
            "trigger_type":       sig.get("trigger_type"),
            "setup":              sig.get("setup"),
            "entry_ref":          entry_ref,
            "stop":               stop,
            "target_1":           target_1,
            "spx_0dte_regime":    sig.get("spx_0dte_regime"),
            "price_at_15m":       p15,
            "price_at_30m":       p30,
            "price_at_60m":       p60,
            "correct_at_15m":     _correct(p15),
            "correct_at_30m":     _correct(p30),
            "correct_at_60m":     _correct(p60),
            **excursions,
        }

        log_signal_outcome(outcome)
        resolved += 1
        log.info("Resolved signal %s (bias=%s, %s)", signal_id, bias, sig.get("timestamp"))

    return resolved
