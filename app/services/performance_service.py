from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.services.trade_logger import read_trades, read_signals


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str))
    except Exception:
        return None


# ── Filtering ─────────────────────────────────────────────────────────────────

def filter_trades(
    trades: List[Dict[str, Any]],
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    last_n: Optional[int] = None,
    setup: Optional[str] = None,
    bias: Optional[str] = None,
) -> List[Dict[str, Any]]:
    result = trades

    if start_date:
        cutoff = datetime.fromisoformat(start_date)
        result = [t for t in result if (_parse_ts(t.get("timestamp")) or datetime.min) >= cutoff]

    if end_date:
        cutoff = datetime.fromisoformat(end_date) + timedelta(days=1)
        result = [t for t in result if (_parse_ts(t.get("timestamp")) or datetime.max) < cutoff]

    if setup:
        result = [t for t in result if t.get("setup") == setup]

    if bias:
        result = [t for t in result if str(t.get("bias", "")).upper() == bias.upper()]

    if last_n is not None and last_n > 0:
        result = sorted(result, key=lambda t: t.get("timestamp", ""))
        result = result[-last_n:]

    return result


# ── Core metrics ──────────────────────────────────────────────────────────────

def summarize_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "avg_pnl": 0.0, "total_pnl": 0.0, "expectancy": 0.0,
            "profit_factor": None, "payoff_ratio": None,
            "avg_r_multiple": None, "sharpe_per_trade": None,
        }

    pnls = [_safe_float(t.get("pnl")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    count = len(pnls)
    win_count = len(wins)
    loss_count = len(losses)

    win_rate = win_count / count if count else 0.0
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / count if count else 0.0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

    profit_factor = (
        round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    )
    payoff_ratio = (
        round(avg_win / abs(avg_loss), 2) if avg_loss != 0 else None
    )

    # R-multiple stats (only for trades that have the field)
    r_vals = [_safe_float(t["r_multiple"]) for t in trades if t.get("r_multiple") is not None]
    avg_r = round(sum(r_vals) / len(r_vals), 2) if r_vals else None

    # Per-trade Sharpe: mean(pnl) / stdev(pnl)
    sharpe = None
    if count >= 2:
        mean_p = avg_pnl
        variance = sum((p - mean_p) ** 2 for p in pnls) / (count - 1)
        stdev = math.sqrt(variance)
        if stdev > 0:
            sharpe = round(mean_p / stdev, 3)

    return {
        "count": count,
        "wins": win_count,
        "losses": loss_count,
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "expectancy": round(expectancy, 2),
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "avg_r_multiple": avg_r,
        "sharpe_per_trade": sharpe,
    }


def group_summary(trades: List[Dict[str, Any]], field: str) -> Dict[str, Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        key = str(t.get(field, "UNKNOWN"))
        groups[key].append(t)
    return {key: summarize_trades(rows) for key, rows in groups.items()}


# ── Equity curve + drawdown ───────────────────────────────────────────────────

def compute_equity_curve(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {}

    sorted_trades = sorted(trades, key=lambda t: t.get("timestamp", ""))
    pnls = [_safe_float(t.get("pnl")) for t in sorted_trades]

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    current_streak = 0  # positive = wins, negative = losses

    for p in pnls:
        cumulative += p
        peak = max(peak, cumulative)
        drawdown = cumulative - peak
        max_drawdown = min(max_drawdown, drawdown)

        if p > 0:
            current_streak = current_streak + 1 if current_streak > 0 else 1
            max_win_streak = max(max_win_streak, current_streak)
        elif p < 0:
            current_streak = current_streak - 1 if current_streak < 0 else -1
            max_loss_streak = max(max_loss_streak, abs(current_streak))

    return {
        "max_drawdown": round(max_drawdown, 2),
        "current_drawdown": round(cumulative - peak, 2),
        "max_consecutive_wins": max_win_streak,
        "max_consecutive_losses": max_loss_streak,
        "current_streak": current_streak,
    }


# ── MAE / MFE excursion stats ─────────────────────────────────────────────────

def compute_excursion_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    mfe_vals = [_safe_float(t["mfe"]) for t in trades if t.get("mfe") is not None]
    mae_vals = [_safe_float(t["mae"]) for t in trades if t.get("mae") is not None]

    if not mfe_vals and not mae_vals:
        return {}

    avg_mfe = round(sum(mfe_vals) / len(mfe_vals), 2) if mfe_vals else None
    avg_mae = round(sum(mae_vals) / len(mae_vals), 2) if mae_vals else None

    # PnL capture: how much of the favorable move did we take?
    pnl_vals = [_safe_float(t.get("pnl")) for t in trades if t.get("mfe") is not None]
    capture = None
    if avg_mfe and avg_mfe > 0 and pnl_vals:
        avg_pnl_for_mfe = sum(pnl_vals) / len(pnl_vals)
        capture = round(avg_pnl_for_mfe / avg_mfe, 4)

    return {
        "avg_mfe": avg_mfe,
        "avg_mae": avg_mae,
        "pnl_capture_ratio": capture,
        "mfe_sample_size": len(mfe_vals),
    }


# ── Time analysis ─────────────────────────────────────────────────────────────

def compute_time_analysis(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    day_groups: Dict[str, List[float]] = defaultdict(list)
    hour_groups: Dict[str, List[float]] = defaultdict(list)
    hold_times: List[float] = []

    _days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for t in trades:
        ts = _parse_ts(t.get("timestamp"))
        if ts:
            day = _days[ts.weekday()]
            day_groups[day].append(_safe_float(t.get("pnl")))
            hour_groups[str(ts.hour)].append(_safe_float(t.get("pnl")))

        hold = t.get("hold_minutes")
        if hold is not None:
            try:
                hold_times.append(float(hold))
            except (TypeError, ValueError):
                pass

    def _win_rate(pnls: List[float]) -> float:
        if not pnls:
            return 0.0
        return round(sum(1 for p in pnls if p > 0) / len(pnls), 4)

    by_day = {
        day: {"trades": len(pnls), "win_rate": _win_rate(pnls), "total_pnl": round(sum(pnls), 2)}
        for day, pnls in day_groups.items()
    }
    by_hour = {
        hour: {"trades": len(pnls), "win_rate": _win_rate(pnls), "total_pnl": round(sum(pnls), 2)}
        for hour, pnls in hour_groups.items()
    }

    avg_hold = round(sum(hold_times) / len(hold_times), 1) if hold_times else None

    if not by_day and not by_hour:
        return {}

    return {
        "by_day_of_week": by_day,
        "by_hour": by_hour,
        "avg_hold_minutes": avg_hold,
    }


# ── Report builder ────────────────────────────────────────────────────────────

def build_performance_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    last_n: Optional[int] = None,
    setup: Optional[str] = None,
    bias: Optional[str] = None,
) -> Dict[str, Any]:
    all_trades = read_trades()
    trades = filter_trades(all_trades, start_date=start_date, end_date=end_date,
                           last_n=last_n, setup=setup, bias=bias)

    report: Dict[str, Any] = {
        "overall": summarize_trades(trades),
        "by_setup": group_summary(trades, "setup"),
        "by_bias": group_summary(trades, "bias"),
        "by_day_type": group_summary(trades, "day_type"),
        "by_trigger_type": group_summary(trades, "trigger_type"),
    }

    equity = compute_equity_curve(trades)
    if equity:
        report["equity"] = equity

    excursion = compute_excursion_stats(trades)
    if excursion:
        report["excursion"] = excursion

    time_analysis = compute_time_analysis(trades)
    if time_analysis:
        report["time_analysis"] = time_analysis

    return report
