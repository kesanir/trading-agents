"""
log_trade_outcome.py — record the result of a manually executed trade.

Usage:
  python log_trade_outcome.py --symbol MNQ --entry 21250 --exit 21310 --pnl 60 --result WIN
  python log_trade_outcome.py --symbol MNQ --entry 21250 --exit 21200 --pnl -50 --result LOSS --notes "stopped out on spike"
  python log_trade_outcome.py --symbol MNQ --entry 21250 --exit 21310 --pnl 60 --result WIN --signal-id a1b2c3d4
  python log_trade_outcome.py --symbol MNQ --entry 21250 --exit 21310 --pnl 60 --result WIN --mae 30 --mfe 85 --entry-time 10:15 --exit-time 10:38
  python log_trade_outcome.py --symbol MNQ --entry 21250 --exit 21310 --pnl 60 --result WIN --no-signal

Context (bias, setup, day_type, spx_0dte_regime, etc.) is pulled automatically
from the most recent signal in signals.jsonl that fired within --signal-minutes
(default 90). Use --signal-id to link directly, or --no-signal to skip.

Derived fields computed automatically when a signal is matched:
  initial_risk_r  — abs(entry_ref - stop) from the signal
  r_multiple      — pnl / initial_risk_r
  hold_minutes    — if --entry-time and --exit-time are provided
"""
from __future__ import annotations

import argparse
import sys
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.trade_logger import log_trade, read_signals


# Fields copied from a matched signal into the trade record
_SIGNAL_FIELDS = [
    "bias",
    "lean",
    "confidence",
    "day_type",
    "trigger_type",
    "setup",
    "entry_ref",
    "stop",
    "target_1",
    "target_2",
    "spx_0dte_regime",
    "spx_0dte_net_gex_b",
    "spx_0dte_zero_gamma",
]


def _find_signal_by_id(signal_id: str) -> Optional[Dict[str, Any]]:
    """Return the signal with the given signal_id."""
    signals = read_signals()
    for sig in reversed(signals):
        if sig.get("signal_id") == signal_id:
            return sig
    return None


def _find_recent_signal(within_minutes: int) -> Optional[Dict[str, Any]]:
    """Return the most recent signal logged within the last `within_minutes`."""
    signals = read_signals()
    if not signals:
        return None

    cutoff = datetime.now() - timedelta(minutes=within_minutes)

    for sig in reversed(signals):
        ts_str = sig.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts >= cutoff:
            return sig

    return None


def _parse_time(time_str: Optional[str]) -> Optional[datetime]:
    """Parse HH:MM or full ISO timestamp into a datetime (today's date if HH:MM)."""
    if not time_str:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(time_str, fmt)
            today = datetime.now().date()
            return datetime.combine(today, t.time())
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(time_str)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Log a completed trade outcome and link it to the triggering signal."
    )
    parser.add_argument("--symbol",      required=True,  help="Instrument (e.g. MNQ, ES)")
    parser.add_argument("--entry",       required=True,  type=float, help="Entry price")
    parser.add_argument("--exit",        required=True,  type=float, help="Exit price")
    parser.add_argument("--pnl",         required=True,  type=float, help="Realised P&L in points or dollars")
    parser.add_argument("--result",      required=True,  choices=["WIN", "LOSS", "BREAKEVEN"],
                        help="Trade result")
    parser.add_argument("--signal-id",   default=None,   help="Exact signal_id to link (e.g. a1b2c3d4)")
    parser.add_argument("--mae",         type=float,     default=None, help="Max adverse excursion in points")
    parser.add_argument("--mfe",         type=float,     default=None, help="Max favorable excursion in points")
    parser.add_argument("--contracts",   type=int,       default=1,    help="Number of contracts traded")
    parser.add_argument("--stop",        type=float,     default=None, help="Actual stop used (overrides signal stop)")
    parser.add_argument("--entry-time",  default=None,   help="Entry time HH:MM or ISO (for hold duration)")
    parser.add_argument("--exit-time",   default=None,   help="Exit time HH:MM or ISO (for hold duration)")
    parser.add_argument("--notes",       default="",     help="Optional free-text notes")
    parser.add_argument("--signal-minutes", type=int, default=90,
                        help="How far back (minutes) to search for a matching signal (default 90)")
    parser.add_argument("--no-signal",   action="store_true",
                        help="Skip signal lookup — log trade numbers only")

    args = parser.parse_args()

    trade: Dict[str, Any] = {
        "symbol":    args.symbol.upper(),
        "entry":     args.entry,
        "exit":      args.exit,
        "pnl":       args.pnl,
        "result":    args.result,
        "contracts": args.contracts,
        "notes":     args.notes,
    }

    if args.mae is not None:
        trade["mae"] = args.mae
    if args.mfe is not None:
        trade["mfe"] = args.mfe
    if args.stop is not None:
        trade["stop_actual"] = args.stop

    # Hold duration
    entry_dt = _parse_time(args.entry_time)
    exit_dt  = _parse_time(args.exit_time)
    if entry_dt and exit_dt and exit_dt > entry_dt:
        trade["entry_time"]   = entry_dt.isoformat()
        trade["exit_time"]    = exit_dt.isoformat()
        trade["hold_minutes"] = round((exit_dt - entry_dt).total_seconds() / 60, 1)

    signal_ts = None
    signal_id_used = None

    if not args.no_signal:
        # Try direct signal_id lookup first
        signal = None
        if args.signal_id:
            signal = _find_signal_by_id(args.signal_id)
            if not signal:
                print(f"WARNING: signal_id '{args.signal_id}' not found — falling back to time-based lookup.")
        if signal is None:
            signal = _find_recent_signal(args.signal_minutes)

        if signal:
            signal_ts = signal.get("timestamp")
            signal_id_used = signal.get("signal_id")
            for field in _SIGNAL_FIELDS:
                if field in signal and signal[field] is not None:
                    trade[field] = signal[field]

            # Compute R-multiple from signal's entry_ref and stop
            entry_ref = float(signal.get("entry_ref") or 0.0)
            stop_level = float(args.stop or signal.get("stop") or 0.0)
            if entry_ref > 0 and stop_level > 0:
                initial_risk_r = abs(entry_ref - stop_level)
                if initial_risk_r > 0:
                    trade["initial_risk_r"] = round(initial_risk_r, 4)
                    trade["r_multiple"]     = round(args.pnl / initial_risk_r, 2)

            print(f"Matched signal: {signal_id_used} from {signal_ts}")
            print(f"  bias={trade.get('bias')}  setup={trade.get('setup')}  "
                  f"day_type={trade.get('day_type')}  trigger={trade.get('trigger_type')}")
            if "r_multiple" in trade:
                print(f"  R-multiple: {trade['r_multiple']:+.2f}R")
        else:
            print(f"No signal found within last {args.signal_minutes} minutes — logging without context.")

    trade["signal_id"]        = signal_id_used
    trade["signal_timestamp"] = signal_ts

    log_trade(trade)

    pnl_sign = "+" if args.pnl >= 0 else ""
    r_str = f"  {trade['r_multiple']:+.2f}R" if "r_multiple" in trade else ""
    print(
        f"\nLogged: {trade['symbol']}  {args.result}  "
        f"entry={args.entry}  exit={args.exit}  pnl={pnl_sign}{args.pnl}{r_str}"
    )


if __name__ == "__main__":
    main()
