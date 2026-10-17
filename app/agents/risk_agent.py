"""
risk_agent.py — Risk context for intraday trigger alerts.

Provides compute_risk_context(trigger_obj, account_config) which produces:
  - position_size_contracts  — how many MNQ contracts to trade given account risk
  - risk_reward_ratio        — target_1 / stop distance
  - daily_pnl               — today's realised P&L from trades.jsonl
  - daily_loss_ok            — True if within max daily loss threshold
  - risk_grade               — A / B / C / F based on R:R, confidence, day_type

Config is read from .env (with safe defaults):
  RISK_ACCOUNT_SIZE        — notional account size in USD (default 25000)
  RISK_PER_TRADE_PCT       — max risk per trade as decimal (default 0.02 = 2%)
  RISK_MAX_DAILY_LOSS      — max daily loss in USD (default 500)
  ALPACA_MAX_RISK_PER_TRADE — fallback if RISK_PER_TRADE_PCT not set
"""
from __future__ import annotations

import os
from datetime import datetime, date
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from app.services.trade_logger import read_trades

load_dotenv()

# MNQ point value: 1 point = $2
_MNQ_POINT_VALUE = 2.0

# NQ/QQQ ratio approximation — used to convert QQQ stop distance to NQ points
# At QQQ ~460, NQ ~20000 → ratio ~43.5; this is recomputed live from trigger data
_DEFAULT_QQQ_TO_NQ_RATIO = 43.5


def _env_float(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _today_pnl(trades: List[Dict[str, Any]]) -> float:
    today = date.today().isoformat()
    total = 0.0
    for t in trades:
        ts = t.get("timestamp", "")
        if ts.startswith(today):
            try:
                total += float(t.get("pnl", 0) or 0)
            except (TypeError, ValueError):
                pass
    return round(total, 2)


def _grade(rr: Optional[float], confidence: str, day_type: str) -> str:
    """
    Risk grade: A (ideal), B (acceptable), C (marginal), F (skip).
    Logic:
      A — RR >= 2.0 AND confidence HIGH AND day_type trend
      B — RR >= 1.5 AND confidence not LOW AND day_type not range/mixed
      C — RR >= 1.0
      F — RR < 1.0 or confidence LOW and day_type range/mixed
    """
    if rr is None:
        return "C"
    bad_day = day_type in ("range", "mixed")
    low_conf = confidence == "LOW"

    if rr >= 2.0 and confidence == "HIGH" and day_type == "trend":
        return "A"
    if rr >= 1.5 and not low_conf and not bad_day:
        return "B"
    if rr >= 1.0 and not (low_conf and bad_day):
        return "C"
    return "F"


def compute_risk_context(
    trigger_obj: Dict[str, Any],
    account_size: Optional[float] = None,
    risk_pct: Optional[float] = None,
    max_daily_loss: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Compute position sizing and risk metrics for the current trigger signal.

    Args:
        trigger_obj: Full output from build_futures_trigger()
        account_size: Override RISK_ACCOUNT_SIZE env var
        risk_pct: Override RISK_PER_TRADE_PCT env var
        max_daily_loss: Override RISK_MAX_DAILY_LOSS env var

    Returns dict with: position_size_contracts, risk_reward_ratio,
        daily_pnl, daily_loss_ok, risk_grade, risk_per_trade_usd
    """
    _account_size   = account_size   or _env_float("RISK_ACCOUNT_SIZE", 25_000.0)
    _risk_pct       = risk_pct       or _env_float("RISK_PER_TRADE_PCT",
                          _env_float("ALPACA_MAX_RISK_PER_TRADE", 0.02))
    _max_daily_loss = max_daily_loss or _env_float("RISK_MAX_DAILY_LOSS", 500.0)

    exec_plan  = trigger_obj.get("execution_plan") or {}
    confidence = trigger_obj.get("confidence", "LOW")
    day_type   = trigger_obj.get("day_type", "watch")

    # Pull NQ levels from execution plan (scalp agent maps QQQ → NQ)
    nq_entry  = exec_plan.get("nq_entry_ref") or exec_plan.get("entry_ref")
    nq_stop   = exec_plan.get("nq_stop")      or exec_plan.get("stop")
    nq_target = exec_plan.get("nq_target_1")  or exec_plan.get("target_1")

    # Try QQQ levels as fallback (used by state_change_runner signal_row)
    if nq_entry is None:
        nq_entry  = exec_plan.get("entry_ref")
        nq_stop   = exec_plan.get("stop")
        nq_target = exec_plan.get("target_1")

    # Position sizing: risk_dollars / (stop_distance_points * point_value)
    risk_dollars = _account_size * _risk_pct
    position_size: Optional[int] = None
    stop_distance: Optional[float] = None
    rr: Optional[float] = None

    if nq_entry and nq_stop:
        try:
            entry_f = float(nq_entry)
            stop_f  = float(nq_stop)
            stop_distance = round(abs(entry_f - stop_f), 2)
            if stop_distance > 0:
                risk_per_contract = stop_distance * _MNQ_POINT_VALUE
                position_size = max(1, int(risk_dollars / risk_per_contract))
        except (TypeError, ValueError):
            pass

    if nq_entry and nq_stop and nq_target:
        try:
            entry_f  = float(nq_entry)
            stop_f   = float(nq_stop)
            target_f = float(nq_target)
            stop_dist   = abs(entry_f - stop_f)
            target_dist = abs(target_f - entry_f)
            if stop_dist > 0:
                rr = round(target_dist / stop_dist, 2)
        except (TypeError, ValueError):
            pass

    # Daily P&L check
    trades    = read_trades()
    daily_pnl = _today_pnl(trades)
    daily_loss_ok = daily_pnl >= -abs(_max_daily_loss)

    grade = _grade(rr, confidence, day_type)

    return {
        "position_size_contracts": position_size,
        "stop_distance_pts":       stop_distance,
        "risk_per_trade_usd":      round(risk_dollars, 2),
        "risk_reward_ratio":       rr,
        "daily_pnl":               daily_pnl,
        "daily_loss_ok":           daily_loss_ok,
        "max_daily_loss":          -abs(_max_daily_loss),
        "risk_grade":              grade,
        "account_size":            _account_size,
        "risk_pct":                _risk_pct,
    }


def risk_check(futures_signal: Optional[Dict[str, Any]]) -> bool:
    """LangGraph-compatible: return True if signal passes basic risk gate."""
    if not futures_signal:
        return False
    ctx = compute_risk_context({"execution_plan": futures_signal,
                                "confidence": futures_signal.get("confidence", "LOW"),
                                "day_type": futures_signal.get("day_type", "watch")})
    return ctx["daily_loss_ok"] and ctx["risk_grade"] != "F"
