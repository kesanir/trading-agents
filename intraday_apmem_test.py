import argparse
import sys
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

from app.services.intraday_apmem_service import build_intraday_apmem_signal
from app.services.state_service import load_state, save_state

ET = ZoneInfo("America/New_York")
WINDOW_START_H = 9
WINDOW_END_H = 12  # exclusive

_VOL_STATE_FILE = "intraday_apmem_test_last.json"


def _now_et() -> datetime:
    return datetime.now(tz=ET)


def _in_window(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return WINDOW_START_H <= now.hour < WINDOW_END_H


def _seconds_until_next_5min(now: datetime) -> float:
    minute = (now.minute // 5 + 1) * 5
    nxt = now.replace(second=0, microsecond=0)
    if minute >= 60:
        nxt = nxt.replace(minute=0) + timedelta(hours=1)
    else:
        nxt = nxt.replace(minute=minute)
    return max(1.0, (nxt - now).total_seconds())


def _seconds_until_window_open(now: datetime) -> float:
    target = now.replace(hour=WINDOW_START_H, minute=0, second=0, microsecond=0)
    if now >= target or now.weekday() >= 5:
        target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
    return (target - now).total_seconds()


def format_for_telegram(d: dict) -> str:
    state_emoji = {"CALM": "🟢", "NORMAL": "🟡", "ELEVATED": "🟠", "STRESS": "🔴"}
    sig_emoji = {"NORMAL": "✅", "CAUTION": "⚠️", "RISK_OFF": "🛑", "AGGRESSIVE": "🚀"}

    state = d.get("apmem_state", "")
    signal = d.get("apmem_signal", "")
    trend = d.get("apmem_trend", 0.0)
    accel = d.get("apmem_accel", 0.0)
    trend_arrow = "▲" if trend > 0 else ("▼" if trend < 0 else "▬")
    accel_arrow = "▲" if accel > 0 else ("▼" if accel < 0 else "▬")

    lines = [
        f"📈 *INTRADAY APMEM — {d.get('symbol','')}*",
        f"_{d.get('as_of','')}  ({d.get('interval','')})_",
        "",
        f"{state_emoji.get(state,'•')} State: *{state}*  (prev: {d.get('prev_apmem_state','')})",
        f"🔄 Transition: `{d.get('apmem_transition','')}`",
        f"{sig_emoji.get(signal,'•')} Signal: *{signal}*",
        f"🎯 Bias: `{d.get('apmem_bias','')}`   Size×: *{d.get('apmem_size_mult','')}*",
        "",
        "*Metrics*",
        f"• Vol:    `{d.get('apmem_vol','')}`",
        f"• Trend:  `{d.get('apmem_trend','')}` {trend_arrow}",
        f"• Accel:  `{d.get('apmem_accel','')}` {accel_arrow}",
    ]
    return "\n".join(lines)


def _format_vol_expansion_alert(d: dict, prev_vol: float) -> str:
    cur_vol = float(d.get("apmem_vol", 0.0))
    delta = cur_vol - prev_vol
    pct = (delta / prev_vol * 100.0) if prev_vol > 0 else 0.0
    return (
        f"⚠️ *VOL EXPANDING — {d.get('symbol','')}*\n"
        f"_{d.get('as_of','')}  ({d.get('interval','')})_\n"
        f"\n"
        f"• Vol: `{prev_vol:.4f}` → `{cur_vol:.4f}`  (+{delta:.4f}, +{pct:.1f}%)\n"
        f"• State: *{d.get('apmem_state','')}*  (prev: {d.get('prev_apmem_state','')})\n"
        f"• Signal: *{d.get('apmem_signal','')}*\n"
        f"• Trend: `{d.get('apmem_trend','')}`   Accel: `{d.get('apmem_accel','')}`"
    )


def run_once(symbol: str, period: str, interval: str, send: bool) -> None:
    sig = build_intraday_apmem_signal(symbol=symbol, period=period, interval=interval)
    d = sig.to_dict()
    msg = format_for_telegram(d)
    print(msg, flush=True)

    if send:
        from app.services.telegram_service import send_telegram_message
        send_telegram_message(msg, parse_mode="Markdown")

    # Vol-expansion alert: fires whenever current apmem_vol exceeds the last
    # persisted value (regardless of --send). Scoped per-symbol so switching
    # symbols doesn't cross-trigger.
    prev = load_state(_VOL_STATE_FILE)
    sym_prev = (prev.get(symbol) or {}) if isinstance(prev, dict) else {}
    prev_vol = float(sym_prev.get("apmem_vol") or 0.0)
    cur_vol = float(d.get("apmem_vol") or 0.0)

    if prev_vol > 0 and cur_vol > prev_vol and cur_vol > 0.20:
        alert = _format_vol_expansion_alert(d, prev_vol)
        print(alert, flush=True)
        try:
            from app.services.telegram_service import send_telegram_message
            send_telegram_message(alert, parse_mode="Markdown")
        except Exception as e:
            print(f"[vol-alert] telegram send failed: {e}", flush=True)

    prev[symbol] = {"apmem_vol": cur_vol, "as_of": d.get("as_of")}
    save_state(_VOL_STATE_FILE, prev)


def run_loop(symbol: str, period: str, interval: str, send: bool) -> None:
    print(
        f"[loop] every 5m, {WINDOW_START_H:02d}:00–{WINDOW_END_H:02d}:00 ET, weekdays",
        flush=True,
    )
    while True:
        now = _now_et()
        if _in_window(now):
            try:
                run_once(symbol, period, interval, send)
            except Exception as e:
                print(f"[loop] run failed: {e}", flush=True)
            sleep_s = _seconds_until_next_5min(_now_et())
        else:
            sleep_s = _seconds_until_window_open(now)
            print(
                f"[loop] outside window ({now:%Y-%m-%d %H:%M %Z}); "
                f"sleeping {sleep_s/60:.1f}m",
                flush=True,
            )
        time.sleep(sleep_s)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="QQQ")
    parser.add_argument("--period", default="5d")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--send", action="store_true", help="send to Telegram")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="auto-run every 5 min from 9:00 to 12:00 ET (weekdays)",
    )
    args = parser.parse_args()

    if args.loop:
        run_loop(args.symbol, args.period, args.interval, args.send)
    else:
        run_once(args.symbol, args.period, args.interval, args.send)