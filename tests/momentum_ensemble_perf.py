"""
Independent per-stream forward-return validator for momentum_ensemble.py picks.

Reads pick ledgers from app/performance/momentum_scanner/, resolves forward
returns from yfinance daily bars, writes per-stream outcome ledgers, and
prints a per-stream report (hit rate, avg return, sharpe).

This script is deliberately disjoint from app.services.signal_outcome_service:
different directory, different bar source (yfinance daily vs Alpaca 1-min),
different horizons (1d/5d/21d vs 15m/30m/60m), different key (pick_id vs signal_id).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import numpy as np
import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = PROJECT_ROOT / "app" / "performance" / "momentum_scanner"

STREAMS = ("momentum", "ml", "ensemble")
HORIZONS_DAYS = (1, 5, 21)


# ---------------------------------------------------------------------------
# JSONL io
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _find_entry_idx(idx: pd.DatetimeIndex, scan_date: str) -> Optional[int]:
    target = pd.to_datetime(scan_date).date()
    for i, ts in enumerate(idx):
        if ts.date() >= target:
            return i
    return None


def resolve_stream(
    stream: str,
    min_age_days: int,
) -> Dict[str, Any]:
    picks_path = LEDGER_DIR / f"{stream}_picks.jsonl"
    outcomes_path = LEDGER_DIR / f"{stream}_outcomes.jsonl"

    picks = read_jsonl(picks_path)
    outcomes = read_jsonl(outcomes_path)
    resolved_ids = {o.get("pick_id") for o in outcomes if o.get("pick_id")}

    today = datetime.now(tz=timezone.utc).date()
    cutoff = today - timedelta(days=min_age_days)

    pending: List[Dict[str, Any]] = []
    for p in picks:
        pid = p.get("pick_id")
        if not pid or pid in resolved_ids:
            continue
        try:
            sdate = datetime.strptime(p["scan_date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if sdate > cutoff:
            continue
        pending.append(p)

    if not pending:
        return {"stream": stream, "pending": 0, "resolved_new": 0}

    tickers = sorted({p["ticker"] for p in pending})
    earliest = min(
        datetime.strptime(p["scan_date"], "%Y-%m-%d").date() for p in pending
    )
    start = (earliest - timedelta(days=5)).strftime("%Y-%m-%d")
    end = (today + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        bars = yf.download(
            tickers,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            group_by="column",
        )
    except Exception as e:
        print(f"[{stream}] yfinance download failed: {e}")
        return {"stream": stream, "pending": len(pending), "resolved_new": 0}

    if bars is None or bars.empty:
        print(f"[{stream}] no bars returned")
        return {"stream": stream, "pending": len(pending), "resolved_new": 0}

    single_ticker = len(tickers) == 1

    def _series(ticker: str, field: str) -> Optional[pd.Series]:
        try:
            if single_ticker:
                return bars[field]
            return bars[field][ticker]
        except Exception:
            return None

    resolved_new = 0
    for p in pending:
        ticker = p["ticker"]
        close = _series(ticker, "Close")
        high = _series(ticker, "High")
        low = _series(ticker, "Low")
        if close is None or close.dropna().empty:
            continue

        close = close.dropna()
        high = high.dropna() if high is not None else None
        low = low.dropna() if low is not None else None

        idx_list = pd.DatetimeIndex(close.index)
        entry_i = _find_entry_idx(idx_list, p["scan_date"])
        if entry_i is None:
            continue
        if entry_i >= len(close):
            continue

        entry_close = float(close.iloc[entry_i])
        if entry_close <= 0:
            continue

        def _fwd_ret(horizon: int) -> Optional[float]:
            j = entry_i + horizon
            if j >= len(close):
                return None
            px = float(close.iloc[j])
            return (px - entry_close) / entry_close

        ret_1d = _fwd_ret(1)
        ret_5d = _fwd_ret(5)
        ret_21d = _fwd_ret(21)

        max_fwd_5d: Optional[float] = None
        min_fwd_5d: Optional[float] = None
        if high is not None and low is not None and len(high) > entry_i + 1:
            end_j = min(entry_i + 5, len(high) - 1)
            hw = high.iloc[entry_i + 1 : end_j + 1]
            lw = low.iloc[entry_i + 1 : end_j + 1]
            if not hw.empty and not lw.empty:
                max_fwd_5d = float(hw.max() - entry_close) / entry_close
                min_fwd_5d = float(lw.min() - entry_close) / entry_close

        have_any = any(r is not None for r in (ret_1d, ret_5d, ret_21d))
        if not have_any:
            continue

        row = {
            "pick_id": p["pick_id"],
            "source": stream,
            "scan_date": p["scan_date"],
            "ticker": ticker,
            "entry_close": round(entry_close, 4),
            "ret_1d": round(ret_1d, 5) if ret_1d is not None else None,
            "ret_5d": round(ret_5d, 5) if ret_5d is not None else None,
            "ret_21d": round(ret_21d, 5) if ret_21d is not None else None,
            "hit_1d": bool(ret_1d > 0) if ret_1d is not None else None,
            "hit_5d": bool(ret_5d > 0) if ret_5d is not None else None,
            "hit_21d": bool(ret_21d > 0) if ret_21d is not None else None,
            "max_fwd_5d": round(max_fwd_5d, 5) if max_fwd_5d is not None else None,
            "min_fwd_5d": round(min_fwd_5d, 5) if min_fwd_5d is not None else None,
            "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        append_jsonl(outcomes_path, row)
        resolved_new += 1

    return {
        "stream": stream,
        "pending": len(pending),
        "resolved_new": resolved_new,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _pct(x: Optional[float]) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "   n/a"
    return f"{x * 100:+6.2f}%"


def _rate(xs: List[bool]) -> str:
    if not xs:
        return "  n/a "
    return f"{(sum(xs) / len(xs)) * 100:5.1f}%"


def report_stream(stream: str) -> None:
    picks_path = LEDGER_DIR / f"{stream}_picks.jsonl"
    outcomes_path = LEDGER_DIR / f"{stream}_outcomes.jsonl"
    picks = read_jsonl(picks_path)
    outcomes = read_jsonl(outcomes_path)

    print(f"\n=== {stream.upper()} STREAM ===")
    print(f"picks: {len(picks)}   resolved: {len(outcomes)}")

    if not outcomes:
        print("  (no outcomes yet — run scanner, wait ≥1 trading day, rerun)")
        return

    for horizon in HORIZONS_DAYS:
        rkey = f"ret_{horizon}d"
        hkey = f"hit_{horizon}d"
        rets = [o[rkey] for o in outcomes if o.get(rkey) is not None]
        hits = [bool(o[hkey]) for o in outcomes if o.get(hkey) is not None]
        if not rets:
            print(f"hit_rate_{horizon}d:   n/a    avg_ret_{horizon}d:   n/a")
            continue
        avg = float(np.mean(rets))
        print(
            f"hit_rate_{horizon}d: {_rate(hits)}   "
            f"avg_ret_{horizon}d: {_pct(avg)}"
        )

    rets_5d = [o["ret_5d"] for o in outcomes if o.get("ret_5d") is not None]
    if len(rets_5d) >= 2:
        arr = np.array(rets_5d, dtype=float)
        std = float(arr.std(ddof=1))
        if std > 0:
            sharpe_5d = float(arr.mean()) / std * math.sqrt(252 / 5)
            print(f"sharpe_5d: {sharpe_5d:.2f}")
        else:
            print("sharpe_5d: n/a (zero variance)")
    else:
        print("sharpe_5d: n/a (need >=2 samples)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--stream",
        choices=list(STREAMS),
        default=None,
        help="resolve+report only this stream (default: all three)",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="skip resolution, just print the report from existing outcomes",
    )
    p.add_argument("--min-age-days", type=int, default=1)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    streams = (args.stream,) if args.stream else STREAMS

    if not args.report_only:
        for s in streams:
            stats = resolve_stream(s, min_age_days=args.min_age_days)
            print(
                f"[resolve {stats['stream']}] "
                f"pending={stats['pending']} new={stats['resolved_new']}"
            )

    for s in streams:
        report_stream(s)


if __name__ == "__main__":
    main()
