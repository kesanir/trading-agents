from __future__ import annotations

import sys

from app.agents.premarket_agent import build_premarket_plan
from app.agents.futures_trigger_agent import build_futures_trigger
from app.data.alpaca_intraday import get_intraday_context
from app.services.formatting_service import (
    format_premarket_plan,
    format_futures_trigger,
)
from app.services.telegram_service import send_telegram_message
from app.services.market_hours_service import (
    is_premarket_window,
    is_market_open_window,
    now_pacific,
)
from app.services.persistence_service import save_report

from app.services.auto_trade_service import pick_trade_candidate
#from app.services.auto_trade_executor import execute_trade
import asyncio

def run_premarket(send_telegram: bool = False) -> str:
    plan = build_premarket_plan("QQQ")
    report = format_premarket_plan(plan)

    # -----------------------------
    # AUTO TRADE DECISION
    # -----------------------------
    from app.services.auto_trade_service import pick_trade_candidate

    trade_intent = pick_trade_candidate(plan)
    plan["trade_intent"] = trade_intent

    if trade_intent:
        report += "\n\n=== TRADE INTENT ===\n"
        report += f"{trade_intent}\n"

    header = f"\n\n##### PREMARKET REPORT | {now_pacific().strftime('%Y-%m-%d %H:%M:%S %Z')} #####\n"
    full_report = header + report

    save_report(
        report_type="premarket",
        raw=plan,
        formatted=full_report,
    )

    try:
        print(full_report)
    except UnicodeEncodeError:
        print(full_report.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))

    if send_telegram:
        send_telegram_message(full_report)
    
   # OPTIONAL: separate alert
    if trade_intent and send_telegram:
        send_telegram_message(
            f"🚨 TRADE INTENT\n{trade_intent}",
            parse_mode=None
        )


    return full_report


def run_after_open(send_telegram: bool = False, nq_price: float | None = None, auto_trade: bool = False) -> str:
    market = get_intraday_context("QQQ", opening_range_minutes=30)

    nq_ctx = None
    if nq_price is None:
        try:
            from app.data.futures_ticks import get_nq_intraday_context
            nq_ctx = get_nq_intraday_context(opening_range_minutes=30)
        except Exception as exc:
            print(f"[futures_ticks] get_nq_intraday_context failed: {exc}")
            nq_ctx = None

    if nq_ctx is not None:
        nq_ref = nq_ctx["price"]
        trigger_price = nq_ctx["price"]
        trigger_vwap = nq_ctx["vwap"]
        trigger_or_high = nq_ctx["or_high"]
        trigger_or_low = nq_ctx["or_low"]
        trigger_source = "nq_native"
        print(
            f"[futures_ticks] Using NQ-native context price={trigger_price} "
            f"vwap={trigger_vwap} or=[{trigger_or_low},{trigger_or_high}]"
        )
    else:
        nq_ref = nq_price if nq_price is not None else market["price"]
        trigger_price = market["price"]
        trigger_vwap = market["vwap"]
        trigger_or_high = market["or_high"]
        trigger_or_low = market["or_low"]
        trigger_source = "qqq_proxy"
        print(
            f"[futures_ticks] Falling back to QQQ proxy price={trigger_price} "
            f"nq_ref={nq_ref}"
        )

    trigger = build_futures_trigger(
        current_price=trigger_price,
        vwap=trigger_vwap,
        or_high=trigger_or_high,
        or_low=trigger_or_low,
        nq_price=nq_ref,
        use_mag7=True,
    )
    trigger["trigger_source"] = trigger_source
    from app.services.intraday_apmem_service import build_intraday_apmem_signal
    from app.services.signal_file_service import write_signal_file, build_signal_data
    report = format_futures_trigger(trigger)
    intraday_apmem = build_intraday_apmem_signal(symbol="QQQ", period="5d", interval="5m")

    # Write NinjaTrader signal file (today's session)
    try:
        sig_data = build_signal_data(trigger, intraday_apmem, market)
        base_contracts = max(1, int(round(float(intraday_apmem.apmem_size_mult or 1))))
        sig_path = write_signal_file(
            instrument="MNQ",
            signal_data=sig_data,
            contracts=base_contracts,
        )
        report += f"\n\n=== NT SIGNAL FILE ===\nWritten: {sig_path}\n"
    except Exception as e:
        report += f"\n\n=== NT SIGNAL FILE ===\nFailed: {e}\n"

    report += "\n\n=== INTRADAY APMEM ===\n"
    report += f"State: {intraday_apmem.apmem_state}\n"
    report += f"Prev State: {intraday_apmem.prev_apmem_state}\n"
    report += f"Transition: {intraday_apmem.apmem_transition}\n"
    report += f"Signal: {intraday_apmem.apmem_signal}\n"
    report += f"Bias: {intraday_apmem.apmem_bias}\n"
    report += f"Vol: {intraday_apmem.apmem_vol}\n"
    report += f"Trend: {intraday_apmem.apmem_trend}\n"
    report += f"Accel: {intraday_apmem.apmem_accel}\n"
    report += f"Size Mult: {intraday_apmem.apmem_size_mult}\n"

    # -------------------------------------------------
    # Load premarket trade intent and confirm execution
    # -------------------------------------------------
    trade_exec_summary = None

    if auto_trade:
        from app.services.trade_intent_service import load_latest_premarket_intent
        from app.services.auto_trade_executor import execute_trade
        from app.services.execution_log_service import (
                already_executed,
                append_log,
            )
        from app.services.open_position_guard import has_open_position
        from alpaca.trading.client import TradingClient
        from core.config import settings
        import asyncio

        def _get_account_equity() -> float:
            client = TradingClient(
                settings.ALPACA_API_KEY,
                settings.ALPACA_SECRET_KEY,
                paper=settings.ALPACA_PAPER,
            )
            acct = client.get_account()
            return float(acct.equity)

        intent = load_latest_premarket_intent()

        if intent:
            trigger_bias = str(trigger.get("bias", "NEUTRAL")).upper()
            trade_triggered = bool(trigger.get("trade_triggered", False))
            intent_direction = str(intent.get("direction", "")).lower()
            ticker = str(intent.get("ticker", "")).upper()

            confirmed = False
            if trade_triggered:
                if intent_direction == "bullish" and trigger_bias == "LONG":
                    confirmed = True
                elif intent_direction == "bearish" and trigger_bias == "SHORT":
                    confirmed = True

            from app.services.execution_log_service import already_executed

            already_executed_flag = already_executed(
                                    ticker=ticker,
                                    direction=intent_direction,
                                    lookback_minutes=180,
                                    )
            already_open = has_open_position(ticker) if ticker else False

            report += "\n\n=== PREMARKET INTENT CHECK ===\n"
            report += f"Intent: {intent}\n"
            report += f"Trigger Bias: {trigger_bias}\n"
            report += f"Trade Triggered: {trade_triggered}\n"
            report += f"Confirmed: {confirmed}\n"
            report += f"Already Executed (Time-based): {already_executed_flag}\n"
            report += f"Already Open Position: {already_open}\n"

            if confirmed and not already_executed_flag and not already_open:
                try:
                    account_equity = _get_account_equity()
                    result, sizing = asyncio.run(
                        execute_trade(intent, account_equity=account_equity)
                    )

                    trade_exec_summary = {
                        "timestamp": now_pacific().isoformat(),
                        "ticker": ticker,
                        "intent": intent,
                        "sizing": sizing,
                        "success": bool(getattr(result, "success", False)),
                        "order_id": getattr(result, "order_id", None),
                        "error": getattr(result, "error", None),
                    }

                    report += "\n=== AUTO EXECUTION ===\n"
                    report += f"Sizing: {sizing}\n"
                    report += f"Success: {trade_exec_summary['success']}\n"
                    if trade_exec_summary["order_id"]:
                        report += f"Order ID: {trade_exec_summary['order_id']}\n"
                    if trade_exec_summary["error"]:
                        report += f"Error: {trade_exec_summary['error']}\n"

                    trigger["auto_execution"] = trade_exec_summary

                    if trade_exec_summary["success"]:
                        append_execution_log({
                            "timestamp": now_pacific().isoformat(),
                            "ticker": ticker,
                            "direction": intent.get("direction"),
                            "strategy": intent.get("strategy"),
                            "score": intent.get("score"),
                            "size_mult": intent.get("size_mult"),
                            "contracts": sizing.get("contracts"),
                            "risk_capital": sizing.get("risk_capital"),
                            "order_id": trade_exec_summary["order_id"],
                            "status": "submitted",
                        })

                except Exception as e:
                    trade_exec_summary = {
                        "timestamp": now_pacific().isoformat(),
                        "ticker": ticker,
                        "intent": intent,
                        "success": False,
                        "error": str(e),
                    }
                    report += "\n=== AUTO EXECUTION ===\n"
                    report += f"Success: False\n"
                    report += f"Error: {e}\n"
                    trigger["auto_execution"] = trade_exec_summary

                    append_execution_log({
                        "timestamp": now_pacific().isoformat(),
                        "ticker": ticker,
                        "direction": intent.get("direction"),
                        "strategy": intent.get("strategy"),
                        "score": intent.get("score"),
                        "size_mult": intent.get("size_mult"),
                        "status": "failed",
                        "error": str(e),
                    })
            else:
                reason = "Intent not confirmed by after-open trigger"
                if already_executed_flag:
                    reason = "Duplicate blocked: symbol already executed today"
                elif already_open:
                    reason = "Duplicate blocked: symbol already has open position"

                trigger["auto_execution"] = {
                    "success": False,
                    "reason": reason,
                    "intent": intent,
                }

                report += "\n=== AUTO EXECUTION ===\n"
                report += f"Success: False\n"
                report += f"Reason: {reason}\n"

        from app.features.position_parser import parse_all_positions, detect_structures
        from app.features.risk_evaluator import evaluate_structure, evaluate_single_position
        from app.services.exit_executor import close_all_legs
        # AUTO EXIT ENGINE
        # -----------------------------
        # -----------------------------
        # STRUCTURE-AWARE EXIT ENGINE
        # -----------------------------
        from alpaca.trading.client import TradingClient
        from core.config import settings

        client = TradingClient(
            settings.ALPACA_API_KEY,
            settings.ALPACA_SECRET_KEY,
            paper=settings.ALPACA_PAPER,
        )

        raw_positions = [p.__dict__ for p in client.get_all_positions()]

        positions = parse_all_positions(raw_positions)
        structures, orphans = detect_structures(positions)

        exit_results = []

        for s in structures:
            assessment = evaluate_structure(s)

            if assessment.should_close:
                res = close_all_legs(s)

                exit_results.append({
                    "underlying": assessment.symbol,
                    "type": assessment.structure_type.value,
                    "reason": assessment.reason.value if assessment.reason else None,
                    "details": assessment.details,
                    "result": res,
                })

        # Handle orphan positions (single legs)
        for p in orphans:
            assessment = evaluate_single_position(p)

            if assessment.should_close:
                try:
                    res = client.close_position(p.symbol)
                    exit_results.append({
                        "symbol": p.symbol,
                        "reason": assessment.reason.value,
                    })
                except Exception as e:
                    exit_results.append({
                        "symbol": p.symbol,
                        "error": str(e),
                    })

        if exit_results:
            report += "\n\n=== AUTO EXITS (STRUCTURE-AWARE) ===\n"
            for r in exit_results:
                report += f"{r}\n"

            trigger["auto_exits"] = exit_results

            if send_telegram:
                send_telegram_message(
                    f"⚠️ AUTO EXITS\n{exit_results}",
                    parse_mode=None
                )
    header = f"\n\n##### AFTER OPEN REPORT | {now_pacific().strftime('%Y-%m-%d %H:%M:%S %Z')} #####\n"
    full_report = header + report

    save_report(
        report_type="trigger",
        raw=trigger,
        formatted=full_report,
    )

    try:
        print(full_report)
    except UnicodeEncodeError:
        print(full_report.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))

    if send_telegram:
        send_telegram_message(full_report, parse_mode=None)

    return full_report
    
    

def main() -> None:
    args = {arg.lower() for arg in sys.argv[1:]}
    send_telegram = "--telegram" in args
    force = "--force" in args
    run_pre = "--premarket" in args
    run_open = "--open" in args
    auto_trade = "--auto-trade" in args

    if not run_pre and not run_open:
        run_pre = True
        run_open = True

    if run_pre:
        if force or is_premarket_window():
            run_premarket(send_telegram=send_telegram)
        else:
            print("Skipped premarket run: outside premarket window.")

    if run_open:
        if force or is_market_open_window():
            run_after_open(send_telegram=send_telegram, nq_price=None, auto_trade=auto_trade)
        else:
            print("Skipped after-open run: outside market window.")


if __name__ == "__main__":
    main()