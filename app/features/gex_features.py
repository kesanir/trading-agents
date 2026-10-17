"""
gex_features.py — SPX 0DTE GEX snapshot and intraday change tracking.

Provides:
  build_spx_0dte_snapshot()  — single bootstrap of SPX 0DTE options (min_dte=0, max_dte=1)
  get_spx_0dte_context()     — current snapshot + delta vs last run (30-min cycle)

Designed to be called from state_change_runner on each 30-min cron tick.
Pre-market: first run captures the opening 0DTE structure.
Intraday:   each subsequent run diffs against the previous to detect
            regime flips, zero-gamma crosses, and net GEX drain/build.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from app.features.gex_core import (
    GEXEngine,
    fast_bootstrap,
    compute_key_levels,
)
from app.services.state_service import load_state, save_state

log = logging.getLogger("gex_features")

_0DTE_STATE_FILE = "spx_0dte_gex.json"
_DEFAULT_TICKER  = "SPY"


def build_spx_0dte_snapshot(ticker: str = _DEFAULT_TICKER) -> Dict[str, Any]:
    """
    Bootstrap SPX today-expiry options and return current GEX levels.
    Returns {} on any failure.

    DTE strategy:
      1. Try max_dte=1 (strict 0DTE — today's expiry only)
      2. If contracts < 10 (pre-market or thin session), retry with max_dte=3
         so the first run of the day still returns usable levels.

    Key outputs (from compute_key_levels):
      net_gex_b        — net GEX in $B (negative = dealer short gamma → vol amplifies)
      regime           — "positive" | "negative"
      regime_strength  — -1…+1
      zero_gamma       — interpolated flip strike
      call_wall        — highest positive GEX strike
      put_wall         — most negative GEX strike
      dist_to_flip_pct — % distance from spot to zero_gamma
      max_dte_used     — actual DTE window used (1 or 3)
    """
    engine = GEXEngine(underlying=ticker)
    max_dte_used = 1

    try:
        fast_bootstrap(engine, ticker, min_dte=0, max_dte=1)
    except Exception as e:
        log.warning("[%s] 0DTE bootstrap failed: %s", ticker, e)
        return {}

    # Fallback: sparse quotes pre-market or early session → widen to 0-3 DTE
    if engine.spot_price <= 0 or len(engine.contract_data) < 10:
        log.info(
            "[%s] Sparse 0DTE data (%d contracts) — retrying with max_dte=3",
            ticker, len(engine.contract_data),
        )
        engine = GEXEngine(underlying=ticker)
        try:
            fast_bootstrap(engine, ticker, min_dte=0, max_dte=3)
            max_dte_used = 3
        except Exception as e:
            log.warning("[%s] 0-3DTE fallback bootstrap failed: %s", ticker, e)
            return {}

    if engine.spot_price <= 0 or len(engine.contract_data) < 5:
        log.warning(
            "[%s] Insufficient GEX data after fallback: spot=%.2f contracts=%d",
            ticker, engine.spot_price, len(engine.contract_data),
        )
        return {}

    gex_map = engine.compute_gex_by_strike()
    levels  = compute_key_levels(gex_map, engine.spot_price)
    if not levels:
        log.warning("[%s] compute_key_levels returned empty for 0DTE chain", ticker)
        return {}

    return {
        "ticker":      ticker,
        "spot":        round(engine.spot_price, 2),
        "contracts":   len(engine.contract_data),
        "max_dte_used": max_dte_used,
        "timestamp":   datetime.now().isoformat(),
        **levels,
    }


def get_spx_0dte_context(ticker: str = _DEFAULT_TICKER) -> Dict[str, Any]:
    """
    Get current SPX 0DTE GEX snapshot and compute delta vs the previous run.
    Persists current snapshot so the next 30-min cycle can diff against it.

    Returned dict:
      current            — full levels for this run (empty if bootstrap failed)
      previous           — levels from last run (empty on first run)
      delta              — changes in net_gex_b, zero_gamma, walls, regime
      regime_flipped     — True if regime changed since last run
      zero_gamma_crossed — True if spot crossed zero_gamma since last run
      error              — present if current snapshot unavailable
    """
    previous = load_state(_0DTE_STATE_FILE)

    # Freshness guard: stale previous (weekend, laptop asleep, ticker change)
    # produces nonsense deltas. Treat anything older than 2h as first-run.
    if previous:
        prev_ts = previous.get("timestamp")
        prev_ticker = previous.get("ticker")
        is_stale = False
        if prev_ticker and prev_ticker != ticker:
            is_stale = True
        elif prev_ts:
            try:
                age = datetime.now() - datetime.fromisoformat(prev_ts)
                if age.total_seconds() > 2 * 3600:
                    is_stale = True
            except (TypeError, ValueError):
                is_stale = True
        if is_stale:
            log.info(
                "[%s] Previous 0DTE snapshot stale (ticker=%s ts=%s) — ignoring for delta",
                ticker, prev_ticker, prev_ts,
            )
            previous = {}

    current  = build_spx_0dte_snapshot(ticker)

    if not current:
        return {
            "current":            {},
            "previous":           previous,
            "delta":              {},
            "regime_flipped":     False,
            "zero_gamma_crossed": False,
            "error":              f"[{ticker}] 0DTE snapshot unavailable",
        }

    save_state(_0DTE_STATE_FILE, current)

    if not previous:
        return {
            "current":            current,
            "previous":           {},
            "delta":              {},
            "regime_flipped":     False,
            "zero_gamma_crossed": False,
        }

    # ── Delta computation ─────────────────────────────────────────────────────

    net_prev  = float(previous.get("net_gex_b") or 0.0)
    net_curr  = float(current.get("net_gex_b")  or 0.0)
    zg_prev   = previous.get("zero_gamma")
    zg_curr   = current.get("zero_gamma")
    spot_prev = float(previous.get("spot") or 0.0)
    spot_curr = float(current.get("spot")  or 0.0)

    regime_flipped = (
        bool(previous.get("regime")) and
        bool(current.get("regime"))  and
        previous["regime"] != current["regime"]
    )

    # zero_gamma_crossed: spot was on one side of the flip, now on the other
    zero_gamma_crossed = False
    if zg_curr is not None and spot_prev > 0 and spot_curr > 0:
        # bool() cast: zg_curr may be a numpy scalar, which makes the comparison
        # return np.bool_ — not JSON-serializable on Python 3.14.
        zero_gamma_crossed = bool((spot_prev > zg_curr) != (spot_curr > zg_curr))

    zg_shift = (
        round(zg_curr - zg_prev, 2)
        if zg_curr is not None and zg_prev is not None else None
    )

    delta: Dict[str, Any] = {
        "net_gex_b_delta":  round(net_curr - net_prev, 4),
        "zero_gamma_prev":  zg_prev,
        "zero_gamma_curr":  zg_curr,
        "zero_gamma_shift": zg_shift,
        "call_wall_prev":   previous.get("call_wall"),
        "call_wall_curr":   current.get("call_wall"),
        "put_wall_prev":    previous.get("put_wall"),
        "put_wall_curr":    current.get("put_wall"),
        "regime_prev":      previous.get("regime"),
        "regime_curr":      current.get("regime"),
    }

    return {
        "current":            current,
        "previous":           previous,
        "delta":              delta,
        "regime_flipped":     regime_flipped,
        "zero_gamma_crossed": zero_gamma_crossed,
    }
