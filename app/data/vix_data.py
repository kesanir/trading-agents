from __future__ import annotations

import yfinance as yf


def get_vix_data() -> dict:
    df = yf.download("^VIX", period="5d", interval="1d", auto_adjust=False)

    if df.empty or len(df) < 2:
        return {
            "vix": None,
            "vix_change": None,
            "vol_regime": "unknown",
        }

    # flatten in case of multi-index
    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)

    df = df.dropna()
    if len(df) < 2:
        return {
            "vix": None,
            "vix_change": None,
            "vol_regime": "unknown",
        }

    last_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    change = round(last_close - prev_close, 2)

    if last_close < 14:
        vol_regime = "low_vol"
    elif last_close < 20:
        vol_regime = "normal_vol"
    else:
        vol_regime = "high_vol"

    return {
        "vix": round(last_close, 2),
        "vix_change": change,
        "vol_regime": vol_regime,
    }