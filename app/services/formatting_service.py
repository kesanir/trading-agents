from __future__ import annotations

from typing import Any, Dict, List, Optional


def format_mag7_summary(summary: dict) -> str:
    leaders = ", ".join(summary["leaders"])
    laggards = ", ".join(summary["laggards"])

    lines = []

    lines.append("=== MAG7 MARKET STRUCTURE ===")
    lines.append(f"Dominant Regime: {summary['dominant_regime'].upper()}")
    lines.append(f"Broad Confirmation: {summary['broad_confirmation']}")
    lines.append("")
    lines.append(f"Leaders: {leaders}")
    lines.append(f"Laggards: {laggards}")
    lines.append(f"Top Pick: {summary['top_pick']}")
    lines.append("")

    lines.append("=== TOP SETUPS ===")

    for c in summary["raw"][:5]:
        lines.append(
            f"{c['ticker']} | {c['regime']} | "
            f"Score: {float(c['score']):.2f} | "
            f"Wall: {c['call_wall']} / {c['put_wall']}"
        )

    return "\n".join(lines)


def _fmt_num(value: Any, digits: int = 2) -> str:
    try:
        if value is None:
            return "N/A"
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_list(items: Optional[List[str]], default: str = "None") -> str:
    if not items:
        return default
    return ", ".join(items)


def _fmt_bool(value: Any) -> str:
    return "YES" if bool(value) else "NO"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1%}"
    except (TypeError, ValueError):
        return "N/A"


def _global_bias_text(vol_score: Any) -> str:
    try:
        score = int(vol_score)
    except (TypeError, ValueError):
        return "Global regime unavailable → use normal risk and let ticker structure lead."

    if score >= 70:
        return "Strong Risk-On → favor LONG continuation setups and allow slightly larger size."
    if score >= 55:
        return "Moderate Risk-On → lean LONG, but still respect walls and opening structure."
    if score <= 30:
        return "Strong Risk-Off → favor SHORT / breakdown setups and defensive posture."
    if score <= 45:
        return "Moderate Risk-Off → lean SHORT, reduce aggressive longs."
    return "Neutral / Chop → reduce size, quick trades, let levels and opening flow lead."


def format_premarket_plan(plan: Dict[str, Any]) -> str:
    index_symbol = plan.get("index_symbol", "QQQ")
    qqq_spot = _fmt_num(plan.get("qqq_spot"), 2)
    qqq_regime = str(plan.get("qqq_regime", "N/A")).upper()
    qqq_score = _fmt_num(plan.get("qqq_score"), 4)
    qqq_strength = _fmt_num(plan.get("qqq_strength"), 4)
    qqq_net_gex_b = _fmt_num(plan.get("qqq_net_gex_b"), 4)
    qqq_call_wall = _fmt_num(plan.get("qqq_call_wall"), 2)
    qqq_put_wall = _fmt_num(plan.get("qqq_put_wall"), 2)
    qqq_zero_gamma = _fmt_num(plan.get("qqq_zero_gamma"), 2)

    # New shared macro regime block
    global_regime = plan.get("global_regime") or {}

    vix = _fmt_num(global_regime.get("vix") if global_regime else plan.get("vix"), 2)
    vix_change = _fmt_num(plan.get("vix_change"), 2)
    vol_regime = str(global_regime.get("regime") or plan.get("vol_regime", "unknown")).upper()
    vol_score = global_regime.get("score")
    vol_bias = str(global_regime.get("bias", "N/A")).upper()
    apmem_vol = _fmt_num(global_regime.get("apmem_vol"), 4)
    apmem_trend = _fmt_num(global_regime.get("apmem_trend"), 4)
    vix_pct = _fmt_num(global_regime.get("vix_percentile"), 2)

    proxy_atm_iv = _fmt_num(plan.get("proxy_atm_iv"), 4)
    expected_move = _fmt_num(plan.get("expected_move"), 2)
    expected_upper = _fmt_num(plan.get("expected_upper"), 2)
    expected_lower = _fmt_num(plan.get("expected_lower"), 2)

    mag7_dom = str(plan.get("mag7_dominant_regime", "N/A")).upper()
    mag7_breadth = str(plan.get("mag7_broad_confirmation", False))
    mag7_leaders = _fmt_list(plan.get("mag7_leaders"))
    mag7_laggards = _fmt_list(plan.get("mag7_laggards"))
    mag7_top_pick = str(plan.get("mag7_top_pick", "N/A"))

    futures_bias = str(plan.get("futures_bias", "N/A")).upper()
    futures_lean = str(plan.get("futures_lean", "NONE")).upper()
    confidence = str(plan.get("confidence", "LOW")).upper()
    day_type = str(plan.get("day_type", "WATCH")).upper()

    market_condition = str(plan.get("market_condition", "N/A")).replace("_", " ").title()
    opening_scenario = str(plan.get("opening_scenario", "N/A")).replace("_", " ").title()
    playbook = str(plan.get("playbook", "N/A")).replace("_", " ").title()

    opening_plan = str(plan.get("opening_plan", "N/A"))
    risk_note = str(plan.get("risk_note", "N/A"))
    notes = plan.get("notes", []) or []
    top3 = plan.get("mag7_top3_setups", []) or []

    lines: List[str] = []
    lines.append("=== PREMARKET PLAN ===")
    lines.append("")
    lines.append("=== INDEX STRUCTURE ===")
    lines.append(f"Symbol: {index_symbol}")
    lines.append(f"Spot: {qqq_spot}")
    lines.append(f"Regime: {qqq_regime}")
    lines.append(f"Score: {qqq_score}")
    lines.append(f"Strength: {qqq_strength}")
    lines.append(f"Net GEX (B): {qqq_net_gex_b}")
    lines.append("")
    lines.append("=== VOLATILITY REGIME (VIX + APMEM) ===")
    lines.append(f"VIX: {vix}  (Pct: {vix_pct})")
    lines.append(f"VIX Change: {vix_change}")
    lines.append(f"APMEM Vol: {apmem_vol}")
    lines.append(f"APMEM Trend: {apmem_trend}")
    lines.append(f"Regime Score: {vol_score if vol_score is not None else 'N/A'}/100")
    lines.append(f"Regime: {vol_regime}")
    lines.append(f"Bias: {vol_bias}")
    lines.append("")
    lines.append("=== QQQ EXPECTED MOVE ===")
    lines.append(f"Proxy ATM IV: {proxy_atm_iv}")
    lines.append(f"Expected Move: {expected_move}")
    lines.append(f"Expected Range: {expected_lower} - {expected_upper}")
    lines.append("")
    lines.append("=== KEY LEVELS ===")
    lines.append(f"Call Wall: {qqq_call_wall}")
    lines.append(f"Put Wall: {qqq_put_wall}")
    lines.append(f"Zero Gamma: {qqq_zero_gamma}")
    lines.append("")
    lines.append("=== MAG7 STRUCTURE ===")
    lines.append(f"Dominant Regime: {mag7_dom}")
    lines.append(f"Broad Confirmation: {mag7_breadth}")
    lines.append(f"Leaders: {mag7_leaders}")
    lines.append(f"Laggards: {mag7_laggards}")
    lines.append(f"Top Pick: {mag7_top_pick}")

    if top3:
        lines.append("")
        lines.append("=== TOP 3 MAG7 SETUPS ===")
        lines.append("Ticker | Regime | Spot | Score | Base/Macro | Size | Call/Put | Exp Move | Range")
        lines.append("-" * 90)

        for row in top3:
            ticker = str(row.get("ticker", "N/A"))
            regime = str(row.get("regime", "N/A")).upper()
            spot = _fmt_num(row.get("spot"), 2)
            score = _fmt_num(row.get("score"), 4)

            base = _fmt_num(row.get("score_base"), 4)
            macro = _fmt_num(row.get("score_macro"), 4)
            size = _fmt_num(row.get("size_mult"), 2)

            call_wall = _fmt_num(row.get("call_wall"), 2)
            put_wall = _fmt_num(row.get("put_wall"), 2)

            exp_move = _fmt_num(row.get("expected_move"), 2)
            exp_low = _fmt_num(row.get("expected_lower"), 2)
            exp_high = _fmt_num(row.get("expected_upper"), 2)

            lines.append(
                f"{ticker:<6} | {regime:<8} | {spot:<7} | {score:<7} | "
                f"{base}/{macro} | {size}x | "
                f"{call_wall}/{put_wall} | {exp_move:<8} | {exp_low}-{exp_high}"
            )

    lines.append("")
    lines.append("=== FUTURES FRAMEWORK ===")
    lines.append(f"Bias: {futures_bias}")
    lines.append(f"Lean: {futures_lean}")
    lines.append(f"Confidence: {confidence}")
    lines.append(f"Day Type: {day_type}")

    lines.append("")
    lines.append("=== GLOBAL TRADE BIAS ===")
    lines.append(_global_bias_text(vol_score))

    lines.append("")
    lines.append("=== MARKET READ ===")
    lines.append(f"Market Condition: {market_condition}")
    lines.append(f"Opening Scenario: {opening_scenario}")
    lines.append(f"Playbook: {playbook}")

    lines.append("")
    lines.append("=== OPENING PLAN ===")
    lines.append(opening_plan)

    lines.append("")
    lines.append("=== RISK NOTE ===")
    lines.append(risk_note)

    lines.append("")
    lines.append("=== REGIME IMPACT ===")
    lines.append(
        f"Macro Score Adjustment Active (Score: {vol_score if vol_score is not None else 'N/A'})"
    )
    lines.append("Ranking adjusted using shared VIX + APMEM overlay")

    if notes:
        lines.append("")
        lines.append("=== NOTES ===")
        for note in notes:
            lines.append(f"- {note}")

    return "\n".join(lines)


def format_futures_trigger(trigger_obj: Dict[str, Any]) -> str:
    """
    Format futures trigger output into a readable trader report.
    """
    bias = str(trigger_obj.get("bias", "N/A")).upper()
    lean = str(trigger_obj.get("lean", "NONE")).upper()
    confidence = str(trigger_obj.get("confidence", "LOW")).upper()
    day_type = str(trigger_obj.get("day_type", "WATCH")).upper()
    scalp_allowed = _fmt_bool(trigger_obj.get("scalp_allowed", False))
    caution = _fmt_bool(trigger_obj.get("caution", True))
    trade_triggered = _fmt_bool(trigger_obj.get("trade_triggered", False))

    trigger = trigger_obj.get("trigger", {}) or {}
    trigger_type = str(trigger.get("trigger_type", "N/A"))
    trigger_reasons = trigger.get("reasons", []) or []

    plan = trigger_obj.get("execution_plan", {}) or {}

    instrument = str(plan.get("instrument", "MNQ"))
    direction = str(plan.get("direction", "N/A")).upper()
    current_price = _fmt_num(plan.get("current_price"), 2)
    vwap = _fmt_num(plan.get("vwap"), 2)
    or_high = _fmt_num(plan.get("or_high"), 2)
    or_low = _fmt_num(plan.get("or_low"), 2)

    entry_ref = _fmt_num(plan.get("entry_ref"), 2)
    stop = _fmt_num(plan.get("stop"), 2)
    target_1 = _fmt_num(plan.get("target_1"), 2)
    target_2 = _fmt_num(plan.get("target_2"), 2)

    setup = str(plan.get("setup", "N/A"))
    entry_note = str(plan.get("entry_note", "N/A"))
    invalidation_note = str(plan.get("invalidation_note", "N/A"))

    all_reasons = trigger_obj.get("reasons", []) or []
    higher = trigger_obj.get("higher_timeframe", {}) or {}
    higher_plan = higher.get("scalp_plan", {}) or {}

    qqq_call_wall = _fmt_num(higher_plan.get("qqq_call_wall"), 2)
    qqq_put_wall = _fmt_num(higher_plan.get("qqq_put_wall"), 2)
    qqq_zero_gamma = _fmt_num(higher_plan.get("qqq_zero_gamma"), 2)

    mag7_pos = _fmt_list(higher_plan.get("mag7_top_positive"))
    mag7_neg = _fmt_list(higher_plan.get("mag7_top_negative"))

    apmem = trigger_obj.get("apmem", {}) or {}
    apmem_state = str(apmem.get("state", "N/A"))
    apmem_bias = str(apmem.get("bias", "N/A"))
    apmem_size_mult = _fmt_num(apmem.get("size_mult"), 2)
    apmem_vol = _fmt_num(apmem.get("vol"), 4)
    apmem_trend = _fmt_num(apmem.get("trend"), 4)
    apmem_accel = _fmt_num(apmem.get("accel"), 4)
    apmem_blocked = _fmt_bool(apmem.get("blocked", False))
    apmem_block_reason = str(apmem.get("block_reason", "None"))

    lines: List[str] = []

    lines.append("=== FUTURES TRIGGER REPORT ===")
    lines.append("")
    lines.append("=== HIGHER TIMEFRAME ===")
    lines.append(f"Bias: {bias}")
    lines.append(f"Lean: {lean}")
    lines.append(f"Confidence: {confidence}")
    lines.append(f"Day Type: {day_type}")
    lines.append(f"Scalp Allowed: {scalp_allowed}")
    lines.append(f"Caution: {caution}")
    lines.append("")
    lines.append("=== TRIGGER STATUS ===")
    lines.append(f"Trade Triggered: {trade_triggered}")
    lines.append(f"Trigger Type: {trigger_type}")
    lines.append("")
    lines.append("=== EXECUTION PLAN ===")
    lines.append(f"Instrument: {instrument}")
    lines.append(f"Direction: {direction}")
    lines.append(f"Current Price: {current_price}")
    lines.append(f"VWAP: {vwap}")
    lines.append(f"OR High: {or_high}")
    lines.append(f"OR Low: {or_low}")
    lines.append(f"Entry Ref: {entry_ref}")
    lines.append(f"Stop: {stop}")
    lines.append(f"Target 1: {target_1}")
    lines.append(f"Target 2: {target_2}")
    lines.append(f"Setup: {setup}")
    lines.append("")
    lines.append("=== QQQ STRUCTURE ===")
    lines.append(f"QQQ Call Wall: {qqq_call_wall}")
    lines.append(f"QQQ Put Wall: {qqq_put_wall}")
    lines.append(f"QQQ Zero Gamma: {qqq_zero_gamma}")
    lines.append("")
    lines.append("=== MAG7 CONFIRMATION ===")
    lines.append(f"Top Positive: {mag7_pos}")
    lines.append(f"Top Negative: {mag7_neg}")
    lines.append("")
    lines.append("=== ENTRY NOTES ===")
    lines.append(entry_note)
    lines.append("")
    lines.append("=== INVALIDATION NOTES ===")
    lines.append(invalidation_note)
    
    lines.append("")
    lines.append("=== APMEM FILTER ===")
    lines.append(f"State: {apmem_state}")
    lines.append(f"Bias: {apmem_bias}")
    lines.append(f"Size Mult: {apmem_size_mult}")
    lines.append(f"Vol: {apmem_vol}")
    lines.append(f"Trend: {apmem_trend}")
    lines.append(f"Accel: {apmem_accel}")
    lines.append(f"Blocked: {apmem_blocked}")
    lines.append(f"Block Reason: {apmem_block_reason}")

    if all_reasons:
        lines.append("")
        lines.append("=== REASONS ===")
        for reason in all_reasons:
            lines.append(f"- {reason}")

    if trigger_reasons:
        lines.append("")
        lines.append("=== TRIGGER REASONS ===")
        for reason in trigger_reasons:
            lines.append(f"- {reason}")

    return "\n".join(lines)


def format_state_change_alert(old_state: Dict[str, Any], new_state: Dict[str, Any]) -> str:
    lines = []
    lines.append("=== STATE CHANGE ALERT ===")
    lines.append("")

    keys = [
        "bias",
        "lean",
        "confidence",
        "day_type",
        "trade_triggered",
        "trigger_type",
    ]

    for key in keys:
        old_val = old_state.get(key, "N/A")
        new_val = new_state.get(key, "N/A")
        if old_val != new_val:
            lines.append(f"{key}: {old_val} → {new_val}")

    lines.append("")
    lines.append("=== CURRENT STATE ===")
    for key in keys:
        lines.append(f"{key}: {new_state.get(key, 'N/A')}")

    return "\n".join(lines)


def format_spx_0dte_gex(ctx: Dict[str, Any]) -> str:
    """
    Format SPX 0DTE GEX context (from get_spx_0dte_context) into a
    compact trader-readable section for inclusion in state change alerts.
    """
    def _n(v: Any, d: int = 2) -> str:
        try:
            return "N/A" if v is None else f"{float(v):.{d}f}"
        except (TypeError, ValueError):
            return "N/A"

    def _delta(v: Any, d: int = 4) -> str:
        try:
            f = float(v)
            sign = "+" if f >= 0 else ""
            return f"{sign}{f:.{d}f}"
        except (TypeError, ValueError):
            return "N/A"

    ticker = (ctx.get("current") or {}).get("ticker") or (ctx.get("previous") or {}).get("ticker") or "SPY"
    lines = [f"=== {ticker} 0DTE GEX ==="]

    error = ctx.get("error")
    if error:
        lines.append(f"Status: UNAVAILABLE ({error})")
        return "\n".join(lines)

    cur = ctx.get("current", {})
    delta = ctx.get("delta", {})
    regime_flipped = ctx.get("regime_flipped", False)
    zero_gamma_crossed = ctx.get("zero_gamma_crossed", False)

    regime_str = str(cur.get("regime", "N/A")).upper()
    if regime_flipped:
        prev_r = str(delta.get("regime_prev", "")).upper()
        regime_str += f"  [FLIPPED from {prev_r}]"

    zg_str = _n(cur.get("zero_gamma"), 2)
    if zero_gamma_crossed:
        zg_str += "  [SPOT CROSSED]"

    zg_shift = delta.get("zero_gamma_shift")
    zg_shift_str = f"  (shift: {_delta(zg_shift, 2)})" if zg_shift is not None else ""

    net_delta_str = (
        f"  ({_delta(delta.get('net_gex_b_delta'), 4)}B)"
        if delta else ""
    )

    cw_prev = delta.get("call_wall_prev")
    cw_curr = delta.get("call_wall_curr") or cur.get("call_wall")
    pw_prev = delta.get("put_wall_prev")
    pw_curr = delta.get("put_wall_curr") or cur.get("put_wall")

    cw_str = _n(cw_curr)
    if cw_prev is not None and cw_prev != cw_curr:
        cw_str += f"  (was {_n(cw_prev)})"

    pw_str = _n(pw_curr)
    if pw_prev is not None and pw_prev != pw_curr:
        pw_str += f"  (was {_n(pw_prev)})"

    lines.append(f"Spot:        {_n(cur.get('spot'), 2)}")
    lines.append(f"Regime:      {regime_str}")
    lines.append(f"Net GEX:     {_n(cur.get('net_gex_b'), 4)}B{net_delta_str}")
    lines.append(f"Zero Gamma:  {zg_str}{zg_shift_str}")
    lines.append(f"Call Wall:   {cw_str}")
    lines.append(f"Put Wall:    {pw_str}")
    lines.append(f"Dist Flip:   {_n(cur.get('dist_to_flip_pct'), 3)}%")
    max_dte = cur.get("max_dte_used", 1)
    contracts_str = str(cur.get("contracts", "N/A"))
    if max_dte and max_dte > 1:
        contracts_str += f"  [fallback: max_dte={max_dte}]"
    lines.append(f"Contracts:   {contracts_str}")

    return "\n".join(lines)


def format_performance_report(report: Dict[str, Any]) -> str:
    overall = report.get("overall", {})
    by_setup = report.get("by_setup", {})
    by_bias = report.get("by_bias", {})
    by_day_type = report.get("by_day_type", {})

    lines = []
    lines.append("=== PERFORMANCE REPORT ===")
    lines.append("")
    lines.append("=== OVERALL ===")
    lines.append(f"Trades: {overall.get('count', 0)}")
    lines.append(f"Wins: {overall.get('wins', 0)}")
    lines.append(f"Losses: {overall.get('losses', 0)}")
    lines.append(f"Win Rate: {overall.get('win_rate', 0.0):.2%}")
    lines.append(f"Avg Win: {overall.get('avg_win', 0.0)}")
    lines.append(f"Avg Loss: {overall.get('avg_loss', 0.0)}")
    lines.append(f"Avg PnL: {overall.get('avg_pnl', 0.0)}")
    lines.append(f"Total PnL: {overall.get('total_pnl', 0.0)}")
    lines.append(f"Expectancy: {overall.get('expectancy', 0.0)}")

    if by_setup:
        lines.append("")
        lines.append("=== BY SETUP ===")
        for k, v in by_setup.items():
            lines.append(
                f"{k}: trades={v.get('count', 0)}, win_rate={v.get('win_rate', 0.0):.2%}, expectancy={v.get('expectancy', 0.0)}"
            )

    if by_bias:
        lines.append("")
        lines.append("=== BY BIAS ===")
        for k, v in by_bias.items():
            lines.append(
                f"{k}: trades={v.get('count', 0)}, win_rate={v.get('win_rate', 0.0):.2%}, expectancy={v.get('expectancy', 0.0)}"
            )

    if by_day_type:
        lines.append("")
        lines.append("=== BY DAY TYPE ===")
        for k, v in by_day_type.items():
            lines.append(
                f"{k}: trades={v.get('count', 0)}, win_rate={v.get('win_rate', 0.0):.2%}, expectancy={v.get('expectancy', 0.0)}"
            )

    by_trigger_type = report.get("by_trigger_type", {})
    if by_trigger_type:
        lines.append("")
        lines.append("=== BY TRIGGER TYPE ===")
        for k, v in by_trigger_type.items():
            lines.append(
                f"{k}: trades={v.get('count', 0)}, win_rate={v.get('win_rate', 0.0):.2%}, expectancy={v.get('expectancy', 0.0)}"
            )

    equity = report.get("equity", {})
    if equity:
        lines.append("")
        lines.append("=== EQUITY & RISK ===")
        pf = overall.get("profit_factor")
        sh = overall.get("sharpe_per_trade")
        lines.append(f"Profit Factor: {pf if pf is not None else 'N/A'}")
        lines.append(f"Sharpe (per-trade): {sh if sh is not None else 'N/A'}")
        lines.append(f"Max Drawdown: {equity.get('max_drawdown', 0.0)}")
        lines.append(f"Current Drawdown: {equity.get('current_drawdown', 0.0)}")
        streak = equity.get("current_streak", 0)
        streak_str = f"+{streak} (winning)" if streak > 0 else (f"{streak} (losing)" if streak < 0 else "0 (flat)")
        lines.append(f"Current Streak: {streak_str}")
        lines.append(f"Max Win Streak: {equity.get('max_consecutive_wins', 0)}")
        lines.append(f"Max Loss Streak: {equity.get('max_consecutive_losses', 0)}")

    avg_r = overall.get("avg_r_multiple")
    if avg_r is not None:
        lines.append("")
        lines.append("=== R-MULTIPLE ===")
        lines.append(f"Avg R: {avg_r:+.2f}R")
        payoff = overall.get("payoff_ratio")
        if payoff is not None:
            lines.append(f"Payoff Ratio: {payoff:.2f}")

    excursion = report.get("excursion", {})
    if excursion:
        lines.append("")
        lines.append("=== TRADE EFFICIENCY (MAE/MFE) ===")
        lines.append(f"Avg MFE: {_fmt_num(excursion.get('avg_mfe'))} pts")
        lines.append(f"Avg MAE: {_fmt_num(excursion.get('avg_mae'))} pts")
        cap = excursion.get("pnl_capture_ratio")
        if cap is not None:
            lines.append(f"PnL Capture: {cap:.1%} of MFE")
        lines.append(f"Sample size: {excursion.get('mfe_sample_size', 0)} trades")

    time_analysis = report.get("time_analysis", {})
    if time_analysis:
        lines.append("")
        lines.append("=== TIME ANALYSIS ===")
        avg_hold = time_analysis.get("avg_hold_minutes")
        if avg_hold is not None:
            lines.append(f"Avg Hold Time: {avg_hold} min")
        by_day = time_analysis.get("by_day_of_week", {})
        if by_day:
            best_day = max(by_day.items(), key=lambda x: x[1].get("win_rate", 0))
            lines.append(
                f"Best Day: {best_day[0]} ({best_day[1].get('win_rate', 0):.1%} WR, "
                f"{best_day[1].get('trades', 0)} trades)"
            )

    return "\n".join(lines)


def format_signal_quality_report(report: Dict[str, Any]) -> str:
    """Format the signal quality / accuracy dashboard produced by signal_quality_service."""
    overall = report.get("overall", {})
    lines = []
    lines.append("=== SIGNAL QUALITY REPORT ===")
    lines.append("")

    total = overall.get("signals", 0)
    resolved = overall.get("resolved", 0)
    lines.append(f"Total signals: {total}  |  Resolved: {resolved}")
    if total:
        lines.append(f"Directional accuracy @ 15m: {_fmt_pct(overall.get('accuracy_15m'))}")
        lines.append(f"Directional accuracy @ 30m: {_fmt_pct(overall.get('accuracy_30m'))}")
        lines.append(f"Directional accuracy @ 60m: {_fmt_pct(overall.get('accuracy_60m'))}")
        lines.append(f"Avg MFE: {_fmt_num(overall.get('avg_mfe'))}  |  Avg MAE: {_fmt_num(overall.get('avg_mae'))}")
        lines.append(f"Target hit rate: {_fmt_pct(overall.get('target_hit_rate'))}  |  Stop hit rate: {_fmt_pct(overall.get('stop_hit_rate'))}")

    def _section(title: str, group: Dict[str, Any]) -> None:
        if not group:
            return
        lines.append("")
        lines.append(f"=== {title} ===")
        for key, stats in sorted(group.items()):
            n = stats.get("signals", 0)
            acc = _fmt_pct(stats.get("accuracy_30m"))
            mfe = _fmt_num(stats.get("avg_mfe"))
            lines.append(f"  {key:<30} signals={n:>4}  acc@30m={acc}  MFE={mfe}")

    _section("BY BIAS", report.get("by_bias", {}))
    _section("BY CONFIDENCE", report.get("by_confidence", {}))
    _section("BY DAY TYPE", report.get("by_day_type", {}))
    _section("BY TRIGGER TYPE", report.get("by_trigger_type", {}))
    _section("BY HOUR OF DAY", report.get("by_hour", {}))
    _section("BY SPX 0DTE REGIME", report.get("by_spx_0dte_regime", {}))

    if not total:
        lines.append("")
        lines.append("No resolved signal outcomes yet. Run state_change_runner.py to generate signals.")

    return "\n".join(lines)


def format_risk_context(ctx: Dict[str, Any]) -> str:
    """Format the risk context block for inclusion in state change alerts."""
    grade = ctx.get("risk_grade", "?")
    rr = ctx.get("risk_reward_ratio")
    size = ctx.get("position_size_contracts")
    risk_usd = ctx.get("risk_per_trade_usd")
    daily_pnl = ctx.get("daily_pnl", 0.0)
    daily_ok = ctx.get("daily_loss_ok", True)
    stop_pts = ctx.get("stop_distance_pts")

    lines = ["=== RISK CONTEXT ==="]
    lines.append(f"Grade: {grade}  |  R:R: {rr if rr is not None else 'N/A'}")
    if size is not None:
        lines.append(f"Size: {size} MNQ contract(s)  (risk ~${risk_usd:.0f}/trade)")
    if stop_pts is not None:
        lines.append(f"Stop distance: {stop_pts:.1f} pts")
    pnl_str = f"+{daily_pnl:.2f}" if daily_pnl >= 0 else f"{daily_pnl:.2f}"
    daily_flag = "" if daily_ok else "  ⚠ NEAR DAILY LIMIT"
    lines.append(f"Today's P&L: {pnl_str}{daily_flag}")
    return "\n".join(lines)