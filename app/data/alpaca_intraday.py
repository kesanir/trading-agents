from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd
from dotenv import load_dotenv

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame


load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")


def _require_keys() -> None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise ValueError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY in .env")


def _client() -> StockHistoricalDataClient:
    _require_keys()
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def _to_dataframe(barset: Any, symbol: str) -> pd.DataFrame:
    """
    Convert alpaca-py bar response to a clean pandas DataFrame.
    """
    df = barset.df.copy()

    if df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "symbol"])

    # alpaca-py commonly returns a multi-index with symbol/timestamp
    df = df.reset_index()

    if "symbol" in df.columns:
        df = df[df["symbol"] == symbol].copy()

    # normalize column names
    rename_map = {
        "timestamp": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    df = df.rename(columns=rename_map)

    keep = [c for c in ["timestamp", "open", "high", "low", "close", "volume", "symbol"] if c in df.columns]
    df = df[keep].copy()
    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


def fetch_intraday_bars(
    symbol: str = "QQQ",
    lookback_minutes: int = 390,
) -> pd.DataFrame:
    """
    Fetch recent 1-minute bars from Alpaca for an equity symbol like QQQ.
    """
    client = _client()

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(minutes=lookback_minutes * 2)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start_dt,
        end=end_dt,
        adjustment="raw",
        feed="iex",  # free feed path; upgrade path can use SIP if available
    )

    barset = client.get_stock_bars(req)
    df = _to_dataframe(barset, symbol)

    if df.empty:
        return df

    # keep only latest lookback window rows
    if len(df) > lookback_minutes:
        df = df.iloc[-lookback_minutes:].reset_index(drop=True)

    return df


def fetch_latest_bar(symbol: str = "QQQ") -> Optional[Dict[str, Any]]:
    """
    Fetch latest bar for quick current price fallback.
    """
    client = _client()

    req = StockLatestBarRequest(symbol_or_symbols=symbol, feed="iex")
    latest = client.get_stock_latest_bar(req)

    # latest may come back keyed by symbol
    bar = latest.get(symbol) if hasattr(latest, "get") else None
    if bar is None:
        return None

    return {
        "symbol": symbol,
        "timestamp": getattr(bar, "timestamp", None),
        "open": float(getattr(bar, "open", 0.0) or 0.0),
        "high": float(getattr(bar, "high", 0.0) or 0.0),
        "low": float(getattr(bar, "low", 0.0) or 0.0),
        "close": float(getattr(bar, "close", 0.0) or 0.0),
        "volume": float(getattr(bar, "volume", 0.0) or 0.0),
    }


def compute_vwap(df: pd.DataFrame) -> Optional[float]:
    if df.empty:
        return None

    price = (df["high"] + df["low"] + df["close"]) / 3.0
    volume = df["volume"].fillna(0)

    vol_sum = volume.cumsum()
    if vol_sum.iloc[-1] == 0:
        return float(df["close"].iloc[-1])

    vwap = (price * volume).cumsum() / vol_sum
    return round(float(vwap.iloc[-1]), 2)


def compute_opening_range(df: pd.DataFrame, minutes: int = 30) -> tuple[Optional[float], Optional[float]]:
    if df.empty:
        return None, None

    n = min(minutes, len(df))
    opening = df.iloc[:n]

    or_high = round(float(opening["high"].max()), 2)
    or_low = round(float(opening["low"].min()), 2)
    return or_high, or_low


def get_intraday_context(symbol: str = "QQQ", opening_range_minutes: int = 30) -> Dict[str, Any]:
    """
    Intraday context for trigger agent:
    - current price
    - vwap
    - opening range high/low
    """
    df = fetch_intraday_bars(symbol=symbol, lookback_minutes=390)

    from app.services.session_calendar import bounds, ET
    now = datetime.now(timezone.utc)
    session = bounds(now.astimezone(ET).date())
    if session is None:
        raise ValueError("No US equity session today")
    stamps = pd.to_datetime(df["timestamp"], utc=True)
    df = df[(stamps >= session[0]) & (stamps < session[1])].copy()
    if df.empty:
        raise ValueError("No current-session intraday bars")
    latest_time = pd.to_datetime(df["timestamp"].iloc[-1], utc=True).to_pydatetime()
    if not timedelta(seconds=-30) <= now - latest_time <= timedelta(minutes=5):
        raise ValueError("Intraday bars are stale or timestamp is in future")

    price = round(float(df["close"].iloc[-1]), 2)
    vwap = compute_vwap(df)
    or_high, or_low = compute_opening_range(df, minutes=opening_range_minutes)

    return {
        "symbol": symbol,
        "price": price,
        "vwap": vwap,
        "or_high": or_high,
        "or_low": or_low,
        "bars": df,
    }