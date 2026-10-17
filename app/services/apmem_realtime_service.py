from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class ApmemRealtimeSignal:
    as_of: str
    apmem_vol: float
    apmem_trend: float
    apmem_accel: float
    apmem_state: str
    apmem_signal: str
    apmem_bias: str
    apmem_size_mult: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _download_spx(period: str = "1y") -> pd.Series:
    df = yf.download("^GSPC", period=period, interval="1d", progress=False, auto_adjust=False)
    if df.empty or "Close" not in df.columns:
        raise ValueError("Failed to download SPX data")
    s = df["Close"].copy()
    s.name = "spx"
    return s


def _compute_apmem_series(
    close: pd.Series,
    rv_window: int = 10,
    delta: float = 1.2,
    omega: float = 0.02,
    alpha: float = 0.25,
    beta: float = 0.65,
    downside_mult: float = 1.8,
) -> pd.DataFrame:
    df = pd.DataFrame(index=close.index)
    df["close"] = close
    df["ret"] = df["close"].pct_change()
    df["rv"] = df["ret"].rolling(rv_window).std() * np.sqrt(252)

    df = df.dropna(subset=["ret", "rv"]).copy()
    df["rv"] = df["rv"].clip(lower=1e-8)
    df["rv_power"] = df["rv"] ** delta

    mu_vals = []
    prev_mu = float(df["rv_power"].iloc[0])

    for i in range(len(df)):
        if i == 0:
            mu_vals.append(prev_mu)
            continue

        shock = float(df["rv_power"].iloc[i - 1])
        prev_ret = float(df["ret"].iloc[i - 1])

        if prev_ret < 0:
            shock *= downside_mult

        new_mu = omega + alpha * shock + beta * prev_mu
        new_mu = max(new_mu, 1e-8)

        mu_vals.append(new_mu)
        prev_mu = new_mu

    df["apmem_power_forecast"] = mu_vals
    df["apmem_vol"] = df["apmem_power_forecast"] ** (1.0 / delta)
    return df


def _trend(series: pd.Series, short_window: int = 5, long_window: int = 10) -> float:
    if len(series) < long_window:
        return 0.0
    short_avg = float(series.tail(short_window).mean())
    prev_avg = float(series.iloc[-long_window:-short_window].mean())
    return short_avg - prev_avg


def _accel(series: pd.Series) -> float:
    if len(series) < 15:
        return 0.0
    t1 = float(series.tail(5).mean())
    t2 = float(series.iloc[-10:-5].mean())
    t3 = float(series.iloc[-15:-10].mean())
    return (t1 - t2) - (t2 - t3)


def _classify_state(apmem_vol: float, apmem_trend: float, apmem_accel: float) -> tuple[str, str, str, float]:
    """
    Returns:
    state, signal, bias, size_mult
    """

    # Suggested starting thresholds.
    # Tune later on your own history.
    high_vol = apmem_vol >= 0.22
    very_high_vol = apmem_vol >= 0.30
    low_vol = apmem_vol <= 0.14

    trend_up = apmem_trend > 0.003
    trend_down = apmem_trend < -0.003
    accel_up = apmem_accel > 0.002
    accel_down = apmem_accel < -0.002

    # 1. Panic
    if very_high_vol and trend_up:
        return "PANIC", "RISK_OFF", "SHORT_OR_WAIT", 0.40

    # 2. Expansion
    if high_vol and (trend_up or accel_up):
        return "VOL_EXPANSION", "DEFENSIVE", "SMALL_SIZE", 0.60

    # 3. Stabilizing after stress
    if high_vol and trend_down:
        return "STABILIZING", "SELECTIVE_LONGS", "LIGHT_LONG", 0.85

    # 4. Recovery sweet spot
    if apmem_vol <= 0.20 and trend_down and accel_down:
        return "RECOVERY", "LONG_RISK_ON", "LONG", 1.20

    # 5. Calm
    if low_vol and not trend_up:
        return "CALM", "NORMAL", "TREND_FOLLOW", 1.00

    return "NEUTRAL", "WAIT", "WAIT", 0.80


def build_apmem_realtime_signal(period: str = "1y") -> ApmemRealtimeSignal:
    close = _download_spx(period=period)
    df = _compute_apmem_series(close)

    apmem_vol = float(df["apmem_vol"].iloc[-1])
    apmem_trend = _trend(df["apmem_vol"])
    apmem_accel = _accel(df["apmem_vol"])

    state, signal, bias, size_mult = _classify_state(
        apmem_vol=apmem_vol,
        apmem_trend=apmem_trend,
        apmem_accel=apmem_accel,
    )

    return ApmemRealtimeSignal(
        as_of=str(df.index[-1].date()),
        apmem_vol=round(apmem_vol, 4),
        apmem_trend=round(apmem_trend, 4),
        apmem_accel=round(apmem_accel, 4),
        apmem_state=state,
        apmem_signal=signal,
        apmem_bias=bias,
        apmem_size_mult=round(size_mult, 2),
    )