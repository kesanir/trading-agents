from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class QqqApmemSignal:
    as_of: str
    symbol: str
    apmem_vol: float
    apmem_trend: float
    apmem_accel: float
    state: str
    signal: str
    bias: str
    size_mult: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _download_close(symbol: str = "QQQ", period: str = "1y") -> pd.Series:
    df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if df.empty or "Close" not in df.columns:
        raise ValueError(f"Failed to download {symbol} data")
    s = df["Close"].copy()
    s.name = symbol
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

    # QQQ tends to move faster than SPY, so keep thresholds a bit more responsive.
    panic = apmem_vol >= 0.30 and apmem_trend > 0.004
    expansion = apmem_vol >= 0.22 and (apmem_trend > 0.002 or apmem_accel > 0.002)
    stabilizing = apmem_vol >= 0.18 and apmem_trend < -0.002
    recovery = apmem_vol <= 0.20 and apmem_trend < -0.003 and apmem_accel < -0.0015
    calm = apmem_vol <= 0.14 and apmem_trend <= 0.001

    if panic:
        return "PANIC", "RISK_OFF", "WAIT_OR_SHORT", 0.40

    if expansion:
        return "VOL_EXPANSION", "DEFENSIVE", "SMALL_SIZE", 0.60

    if stabilizing:
        return "STABILIZING", "SELECTIVE_LONGS", "LIGHT_LONG", 0.85

    if recovery:
        return "RECOVERY", "LONG_RISK_ON", "LONG", 1.20

    if calm:
        return "CALM", "NORMAL", "TREND_FOLLOW", 1.00

    return "NEUTRAL", "WAIT", "WAIT", 0.80


def build_qqq_apmem_signal(period: str = "1y") -> QqqApmemSignal:
    close = _download_close("QQQ", period=period)
    df = _compute_apmem_series(close)

    apmem_vol = float(df["apmem_vol"].iloc[-1])
    apmem_trend = _trend(df["apmem_vol"])
    apmem_accel = _accel(df["apmem_vol"])

    state, signal, bias, size_mult = _classify_state(
        apmem_vol=apmem_vol,
        apmem_trend=apmem_trend,
        apmem_accel=apmem_accel,
    )

    return QqqApmemSignal(
        as_of=str(df.index[-1].date()),
        symbol="QQQ",
        apmem_vol=round(apmem_vol, 4),
        apmem_trend=round(apmem_trend, 4),
        apmem_accel=round(apmem_accel, 4),
        state=state,
        signal=signal,
        bias=bias,
        size_mult=round(size_mult, 2),
    )