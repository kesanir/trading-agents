import yfinance as yf
import pandas as pd
from datetime import datetime


def get_nq_price():
    # Continuous futures symbol (not perfect but fine for now)
    df = yf.download("^NDX", period="1d", interval="1m")
    return float(df["Close"].iloc[-1])


def compute_vwap(df: pd.DataFrame):
    price = (df["High"] + df["Low"] + df["Close"]) / 3
    volume = df["Volume"]

    vwap = (price * volume).cumsum() / volume.cumsum()
    return float(vwap.iloc[-1])


def compute_opening_range(df: pd.DataFrame, minutes: int = 30):
    start = df.iloc[:minutes]
    return float(start["High"].max()), float(start["Low"].min())


def get_intraday_context(symbol="^NDX"):
    df = yf.download(symbol, period="1d", interval="1m")

    # Fix: flatten columns if multi-index
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Ensure numeric
    df = df.dropna()

    # Extract scalar safely
    price = float(df["Close"].values[-1])

    vwap = compute_vwap(df)
    or_high, or_low = compute_opening_range(df)

    return {
        "price": price,
        "vwap": vwap,
        "or_high": or_high,
        "or_low": or_low,
    }