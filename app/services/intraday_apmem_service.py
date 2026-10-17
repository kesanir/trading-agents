from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd
import yfinance as yf


STATE_FILE = Path("app/state/intraday_apmem_state.json")


@dataclass
class IntradayApmemSignal:
    as_of: str
    symbol: str
    interval: str
    apmem_vol: float
    apmem_trend: float
    apmem_accel: float
    apmem_state: str
    prev_apmem_state: str
    apmem_transition: str
    apmem_signal: str
    apmem_bias: str
    apmem_size_mult: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def _load_prev_payload() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_prev_state() -> str:
    data = _load_prev_payload()
    return str(data.get("apmem_state", "UNKNOWN")).upper()


def _load_prev_vol() -> float | None:
    data = _load_prev_payload()
    try:
        payload = data.get("payload", {}) or {}
        v = payload.get("apmem_vol")
        return float(v) if v is not None else None
    except Exception:
        return None


def _save_state(state: str, payload: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "apmem_state": state,
        "payload": payload,
    }
    STATE_FILE.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")


def _download_intraday_close(symbol: str = "QQQ", period: str = "5d", interval: str = "5m") -> pd.Series:
    df = yf.download(symbol, period=period, interval=interval, progress=False, auto_adjust=False)

    if df.empty:
        raise ValueError(f"No intraday data downloaded for {symbol}")

    # Handle MultiIndex columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        if ("Close", symbol) in df.columns:
            close = df[("Close", symbol)].copy()
        elif ("Adj Close", symbol) in df.columns:
            close = df[("Adj Close", symbol)].copy()
        else:
            close_cols = [c for c in df.columns if str(c[0]).lower() == "close"]
            if not close_cols:
                raise ValueError(f"Could not find close column in intraday data: {df.columns}")
            close = df[close_cols[0]].copy()
        return pd.Series(close).dropna()

    cols_lower = {str(c).lower(): c for c in df.columns}
    if "close" in cols_lower:
        close = df[cols_lower["close"]].copy()
    elif "adj close" in cols_lower:
        close = df[cols_lower["adj close"]].copy()
    else:
        raise ValueError(f"Could not find close column in intraday data: {list(df.columns)}")

    return pd.Series(close).dropna()


def _compute_apmem_series(
    close: pd.Series,
    rv_window: int = 12,
    delta: float = 1.1,
    omega: float = 0.01,
    alpha: float = 0.30,
    beta: float = 0.58,
    downside_mult: float = 2.0,
    annualization_factor: float = 78 * 252,  # 5m bars in US session
) -> pd.DataFrame:
    if isinstance(close, pd.DataFrame):
        if close.shape[1] != 1:
            raise ValueError(f"Expected 1 close column, got {close.shape[1]} columns")
        close = close.iloc[:, 0]

    close = pd.Series(close).astype(float).dropna().copy()
    close.name = "close"

    df = pd.DataFrame(index=close.index)
    df["close"] = close.to_numpy()
    #df["ret"] = df["close"].pct_change()
    df["ret"] = np.log(df["close"] / df["close"].shift(1))
    df["rv"] = df["ret"].rolling(rv_window).std() * np.sqrt(annualization_factor)

    df = df.dropna(subset=["ret", "rv"]).copy()
    if df.empty:
        raise ValueError("Empty intraday series after RV calculation")

    df["rv"] = df["rv"].clip(lower=1e-8)
    df["rv_power"] = df["rv"] ** delta

    ret_arr = df["ret"].to_numpy(dtype=float)
    rv_power_arr = df["rv_power"].to_numpy(dtype=float)

    mu_vals = np.zeros(len(df), dtype=float)
    prev_mu = float(rv_power_arr[0])
    mu_vals[0] = prev_mu

    for i in range(1, len(df)):
        shock = float(rv_power_arr[i - 1])
        prev_ret = float(ret_arr[i - 1])

        if prev_ret < 0:
            shock *= downside_mult

        new_mu = omega + alpha * shock + beta * prev_mu
        new_mu = max(new_mu, 1e-8)

        mu_vals[i] = new_mu
        prev_mu = new_mu

    df["apmem_power_forecast"] = mu_vals
    df["apmem_vol"] = df["apmem_power_forecast"] ** (1.0 / delta)
    return df


def _trend(series: pd.Series, short_window: int = 6, long_window: int = 12) -> float:
    if len(series) < long_window:
        return 0.0

    short_avg = float(series.tail(short_window).mean())
    prev_avg = float(series.iloc[-long_window:-short_window].mean())

    if prev_avg == 0:
        return 0.0

    return (short_avg - prev_avg) / prev_avg


def _accel(series: pd.Series) -> float:
    if len(series) < 18:
        return 0.0

    t1 = float(series.tail(6).mean())
    t2 = float(series.iloc[-12:-6].mean())
    t3 = float(series.iloc[-18:-12].mean())

    if t2 == 0:
        return 0.0

    return ((t1 - t2) - (t2 - t3)) / t2


def _classify_state(
    apmem_vol: float,
    apmem_trend: float,
    apmem_accel: float,
    prev_apmem_vol: float | None = None,
) -> tuple[str, str, str, float]:
    """
    Returns:
    state, signal, bias, size_mult
    """

    vol_delta = 0.0
    vol_pct_change = 0.0

    if prev_apmem_vol not in (None, 0):
        vol_delta = apmem_vol - prev_apmem_vol
        vol_pct_change = vol_delta / prev_apmem_vol

    meaningful_expand = vol_delta >= 0.003 and vol_pct_change >= 0.01
    meaningful_contract = vol_delta <= -0.003 and vol_pct_change <= -0.01

    if apmem_vol >= 0.36 and apmem_trend > 0.010 and meaningful_expand:
        return "PANIC", "RISK_OFF", "WAIT_OR_SHORT", 0.30

    if apmem_vol >= 0.24 and (apmem_trend > 0.005 or apmem_accel > 0.003):
        if meaningful_expand:
            return "VOL_EXPANSION", "DEFENSIVE", "SMALL_SIZE", 0.55
        return "SLIGHT_EXPANSION", "CAUTION", "WAIT", 0.70

    if apmem_vol >= 0.18 and apmem_trend < -0.004 and meaningful_contract:
        return "STABILIZING", "SELECTIVE_LONGS", "LIGHT_LONG", 0.85

    if (
        apmem_vol <= 0.18
        and apmem_trend < -0.005
        and apmem_accel < -0.002
        and meaningful_contract
    ):
        return "RECOVERY", "LONG_RISK_ON", "LONG", 1.15

    if apmem_vol <= 0.12 and apmem_trend <= 0.001:
        return "CALM", "NORMAL", "TREND_FOLLOW", 1.00

    return "NEUTRAL", "WAIT", "WAIT", 0.80


def _transition(prev_state: str, state: str) -> str:
    prev_state = str(prev_state).upper()
    state = str(state).upper()
    if prev_state == state:
        return f"{state}_HOLD"
    return f"{prev_state}_TO_{state}"


def build_intraday_apmem_signal(
    symbol: str = "QQQ",
    period: str = "5d",
    interval: str = "5m",
) -> IntradayApmemSignal:
    close = _download_intraday_close(symbol=symbol, period=period, interval=interval)
    df = _compute_apmem_series(close)

    apmem_vol = float(df["apmem_vol"].iloc[-1])
    apmem_trend = _trend(df["apmem_vol"])
    apmem_accel = _accel(df["apmem_vol"])
    prev_apmem_vol = _load_prev_vol()

    state, signal, bias, size_mult = _classify_state(
        apmem_vol=apmem_vol,
        apmem_trend=apmem_trend,
        apmem_accel=apmem_accel,
        prev_apmem_vol=prev_apmem_vol,
    )

    prev_state = _load_prev_state()
    trans = _transition(prev_state, state)

    result = IntradayApmemSignal(
        as_of=str(df.index[-1]),
        symbol=symbol,
        interval=interval,
        apmem_vol=round(apmem_vol, 4),
        apmem_trend=round(apmem_trend, 4),
        apmem_accel=round(apmem_accel, 4),
        apmem_state=state,
        prev_apmem_state=prev_state,
        apmem_transition=trans,
        apmem_signal=signal,
        apmem_bias=bias,
        apmem_size_mult=round(size_mult, 2),
    )

    _save_state(state, result.to_dict())
    return result