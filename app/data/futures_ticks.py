"""
Databento-backed NQ futures data helpers.

Public functions:
  - get_nq_last() -> float | None
      Latest NQ front-month price (1-min OHLCV close).
  - get_nq_intraday_context(opening_range_minutes=30) -> dict | None
      Session price + VWAP + opening range computed from NQ 1-min bars.
  - get_nq_overnight_context() -> dict | None
      Globex overnight high/low + prior RTH close + gap_pct.

All functions return None on any failure (missing key, network, empty data)
so callers can fall back to existing QQQ-based defaults.

Symbology:
  Dataset = GLBX.MDP3 (CME Globex)
  Symbol  = NQ.c.0  with stype_in="continuous"  (front-month auto-roll)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

DATABENTO_API_KEY = os.getenv("DATABENTO_API_KEY")
DATASET = "GLBX.MDP3"
SYMBOL = "NQ.c.0"
STYPE_IN = "continuous"

ET = ZoneInfo("America/New_York")

RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


def _client():
    if not DATABENTO_API_KEY:
        logger.warning("DATABENTO_API_KEY not set; futures data unavailable")
        return None
    try:
        import databento as db
        return db.Historical(key=DATABENTO_API_KEY)
    except Exception as exc:
        logger.warning("Failed to construct Databento client: %s", exc)
        return None


def _dataset_end(client) -> Optional[datetime]:
    """Return the latest UTC timestamp available for DATASET, or None."""
    try:
        rng = client.metadata.get_dataset_range(dataset=DATASET)
        end_str = rng.get("end") if isinstance(rng, dict) else None
        if not end_str:
            return None
        ts = pd.Timestamp(end_str)
        return ts.tz_convert("UTC").to_pydatetime() if ts.tzinfo else ts.tz_localize("UTC").to_pydatetime()
    except Exception as exc:
        logger.warning("Databento dataset range lookup failed: %s", exc)
        return None


def _fetch_ohlcv_1m(start: datetime, end: datetime) -> Optional[pd.DataFrame]:
    """Fetch NQ 1-min OHLCV between UTC start/end. Returns None on failure.

    Clips `end` to the dataset's available range to avoid 422 errors when the
    requested window extends past the historical feed's small latency.
    """
    client = _client()
    if client is None:
        return None
    avail_end = _dataset_end(client)
    if avail_end is not None and end > avail_end:
        end = avail_end
    if end <= start:
        logger.warning(
            "Databento window collapsed after clipping: start=%s end=%s (avail_end=%s)",
            start, end, avail_end,
        )
        return None
    try:
        data = client.timeseries.get_range(
            dataset=DATASET,
            symbols=[SYMBOL],
            stype_in=STYPE_IN,
            schema="ohlcv-1m",
            start=start,
            end=end,
        )
        df = data.to_df()
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:
        logger.warning("Databento ohlcv-1m fetch failed: %s", exc)
        return None


def get_nq_last() -> Optional[float]:
    """Return the most recent NQ front-month 1-min bar close, or None."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=15)
    df = _fetch_ohlcv_1m(start, end)
    if df is None:
        return None
    try:
        return float(df["close"].iloc[-1])
    except Exception as exc:
        logger.warning("get_nq_last: bad frame: %s", exc)
        return None


def _compute_vwap(df: pd.DataFrame) -> Optional[float]:
    if df.empty:
        return None
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].fillna(0)
    vol_sum = vol.cumsum()
    if vol_sum.iloc[-1] == 0:
        return float(df["close"].iloc[-1])
    vwap = (typical * vol).cumsum() / vol_sum
    return round(float(vwap.iloc[-1]), 2)


def _compute_opening_range(df: pd.DataFrame, minutes: int) -> tuple[Optional[float], Optional[float]]:
    if df.empty:
        return None, None
    n = min(minutes, len(df))
    head = df.iloc[:n]
    return round(float(head["high"].max()), 2), round(float(head["low"].min()), 2)


def get_nq_intraday_context(opening_range_minutes: int = 30) -> Optional[Dict[str, Any]]:
    """
    Return {price, vwap, or_high, or_low, bars} for the current RTH session.
    If called before RTH open, returns None.
    """
    now_et = datetime.now(ET)
    if now_et.weekday() >= 5:
        return None
    session_open_et = datetime.combine(now_et.date(), RTH_OPEN, tzinfo=ET)
    if now_et < session_open_et:
        return None

    start_utc = session_open_et.astimezone(timezone.utc)
    end_utc = now_et.astimezone(timezone.utc)

    df = _fetch_ohlcv_1m(start_utc, end_utc)
    if df is None or df.empty:
        return None

    price = round(float(df["close"].iloc[-1]), 2)
    vwap = _compute_vwap(df)
    or_high, or_low = _compute_opening_range(df, opening_range_minutes)

    return {
        "symbol": SYMBOL,
        "price": price,
        "vwap": vwap,
        "or_high": or_high,
        "or_low": or_low,
        "bars": df,
    }


def get_nq_overnight_context() -> Optional[Dict[str, Any]]:
    """
    Return {overnight_high, overnight_low, overnight_close, prior_rth_close, gap_pct}.
    Window = prior RTH close (16:00 ET prev day) -> today's RTH open (09:30 ET).
    """
    now_et = datetime.now(ET)
    today = now_et.date()
    yesterday = today - timedelta(days=1)
    if now_et.weekday() == 0:
        yesterday = today - timedelta(days=3)

    prior_rth_close_et = datetime.combine(yesterday, RTH_CLOSE, tzinfo=ET)
    today_rth_open_et = datetime.combine(today, RTH_OPEN, tzinfo=ET)

    start_utc = prior_rth_close_et.astimezone(timezone.utc)
    end_utc = today_rth_open_et.astimezone(timezone.utc)

    df = _fetch_ohlcv_1m(start_utc, end_utc)
    if df is None or df.empty:
        return None

    overnight_high = round(float(df["high"].max()), 2)
    overnight_low = round(float(df["low"].min()), 2)
    overnight_close = round(float(df["close"].iloc[-1]), 2)
    prior_rth_close = round(float(df["close"].iloc[0]), 2)
    gap_pct = round((overnight_close - prior_rth_close) / prior_rth_close * 100, 3) if prior_rth_close else 0.0

    return {
        "overnight_high": overnight_high,
        "overnight_low": overnight_low,
        "overnight_close": overnight_close,
        "prior_rth_close": prior_rth_close,
        "gap_pct": gap_pct,
    }
