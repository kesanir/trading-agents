from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Any
import numpy as np
import pandas as pd
import yfinance as yf
import json
from pathlib import Path

STATE_FILE = Path("app/state/apmem_state.json")

@dataclass
class VixApmemSignal:
    as_of: str
    vix: float
    vix_percentile: float
    apmem_vol: float
    apmem_trend: float
    apmem_accel: float
    apmem_state: str
    apmem_signal: str
    apmem_size_mult: float
    score: int
    regime: str
    bias: str
    spy_trend: float
    qqq_trend: float
    prev_state: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def _download_close(symbol: str, period: str = "1y") -> pd.Series:
    df = yf.download(symbol, period=period, interval="1d", progress=False, auto_adjust=False)
    if df.empty or "Close" not in df.columns:
        raise ValueError(f"Failed to download close data for {symbol}")
    s = df["Close"].copy()
    s.name = symbol
    return s


def _build_base_df(period: str = "1y") -> pd.DataFrame:
    spx = _download_close("^GSPC", period)
    vix = _download_close("^VIX", period)
    spy = _download_close("SPY", period)
    qqq = _download_close("QQQ", period)

    df = pd.concat([spx, vix, spy, qqq], axis=1)
    df.columns = ["spx", "vix", "spy", "qqq"]
    df = df.dropna().copy()

    if len(df) < 60:
        raise ValueError("Not enough overlapping data to compute VIX+APMEM signal")

    return df


def _compute_apmem(
    df: pd.DataFrame,
    rv_window: int = 10,
    delta: float = 1.1,
    omega: float = 0.01,
    alpha: float = 0.30,
    beta: float = 0.58,
    downside_mult: float = 2,
) -> pd.DataFrame:
    out = df.copy()

    out["ret"] = out["spx"].pct_change()
    out["rv"] = out["ret"].rolling(rv_window).std() * np.sqrt(252)

    out = out.dropna(subset=["ret", "rv"]).copy()
    out["rv"] = out["rv"].clip(lower=1e-8)
    out["rv_power"] = out["rv"] ** delta

    mu_vals = []
    prev_mu = float(out["rv_power"].iloc[0])

    for i in range(len(out)):
        if i == 0:
            mu_vals.append(prev_mu)
            continue

        shock = float(out["rv_power"].iloc[i - 1])
        prev_ret = float(out["ret"].iloc[i - 1])

        if prev_ret < 0:
            shock *= downside_mult

        new_mu = omega + alpha * shock + beta * prev_mu
        new_mu = max(new_mu, 1e-8)

        mu_vals.append(new_mu)
        prev_mu = new_mu

    out["apmem_power_forecast"] = mu_vals
    out["apmem_vol"] = out["apmem_power_forecast"] ** (1.0 / delta)
    return out


def _rolling_percentile_last(series: pd.Series, window: int = 252) -> pd.Series:
    def _pct(x: np.ndarray) -> float:
        s = pd.Series(x)
        return float(s.rank(pct=True).iloc[-1])

    min_periods = min(window, 30)
    return series.rolling(window=window, min_periods=min_periods).apply(_pct, raw=True)


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

def _classify_score(score: int) -> tuple[str, str]:
    if score >= 70:
        return "STRONG_LONG", "LONG"
    if score >= 55:
        return "LEAN_LONG", "LIGHT_LONG"
    if score <= 30:
        return "STRONG_SHORT", "SHORT"
    if score <= 45:
        return "LEAN_SHORT", "LIGHT_SHORT"
    return "NEUTRAL", "WAIT"
def _classify_apmem_state(apmem_vol: float, apmem_trend: float, apmem_accel: float) -> tuple[str, str, float]:
    """
    Returns:
    apmem_state, apmem_signal, apmem_size_mult
    """

    # Tuned around your walk-forward robust parameter family
    if apmem_vol >= 0.28 and apmem_trend > 0.004:
        return "PANIC", "RISK_OFF", 0.40

    if apmem_vol >= 0.21 and (apmem_trend > 0.0025 or apmem_accel > 0.0015):
        return "VOL_EXPANSION", "DEFENSIVE", 0.60

    if apmem_vol >= 0.17 and apmem_trend < -0.0025:
        return "STABILIZING", "SELECTIVE_LONGS", 0.85

    if apmem_vol <= 0.19 and apmem_trend < -0.0035 and apmem_accel < -0.0010:
        return "RECOVERY", "LONG_RISK_ON", 1.20

    if apmem_vol <= 0.14 and apmem_trend <= 0.001:
        return "CALM", "NORMAL", 1.00

    return "NEUTRAL", "WAIT", 0.80

def build_vix_apmem_signal(period: str = "1y") -> VixApmemSignal:
    df = _build_base_df(period=period)
    df = _compute_apmem(df)
    df["vix_pct"] = _rolling_percentile_last(df["vix"])

    latest = df.iloc[-1]
    vix = float(latest["vix"])
    vix_pct = float(latest["vix_pct"]) if pd.notna(latest["vix_pct"]) else 0.50
    apmem_vol = float(latest["apmem_vol"])

    apmem_trend = _trend(df["apmem_vol"])
    apmem_accel = _accel(df["apmem_vol"])
    spy_trend = _trend(df["spy"])
    qqq_trend = _trend(df["qqq"])

    apmem_state, apmem_signal, apmem_size_mult = _classify_apmem_state(
        apmem_vol=apmem_vol,
        apmem_trend=apmem_trend,
        apmem_accel=apmem_accel,
    )

    score = 50

    # VIX absolute
    if vix > 22:
        score -= 15
    elif vix < 15:
        score += 15
    elif 18 <= vix <= 22:
        score -= 5

    # VIX percentile
    if vix_pct > 0.80:
        score -= 10
    elif vix_pct < 0.20:
        score += 10

    # APMEM trend
    if apmem_trend > 0.003:
        score -= 20
    elif apmem_trend < -0.003:
        score += 20
    elif apmem_trend > 0:
        score -= 8
    elif apmem_trend < 0:
        score += 8

    # APMEM state overlay
    if apmem_state == "PANIC":
        score -= 15
    elif apmem_state == "VOL_EXPANSION":
        score -= 8
    elif apmem_state == "RECOVERY":
        score += 12
    elif apmem_state == "CALM":
        score += 4

    # SPY / QQQ confirmation
    if spy_trend > 0 and qqq_trend > 0:
        score += 10
    elif spy_trend < 0 and qqq_trend < 0:
        score -= 10

    score = max(0, min(100, int(round(score))))
    regime, bias = _classify_score(score)
    prev_state = load_prev_apmem_state()
    save_apmem_state(apmem_state)
    

    return VixApmemSignal(
        as_of=str(df.index[-1].date()),
        vix=round(vix, 2),
        vix_percentile=round(vix_pct, 2),
        apmem_vol=round(apmem_vol, 4),
        apmem_trend=round(apmem_trend, 4),
        apmem_accel=round(apmem_accel, 4),
        apmem_state=apmem_state,
        apmem_signal=apmem_signal,
        apmem_size_mult=round(apmem_size_mult, 2),
        score=score,
        regime=regime,
        bias=bias,
        spy_trend=round(spy_trend, 4),
        qqq_trend=round(qqq_trend, 4),
        prev_state=prev_state,
    )

def save_apmem_state(state: str):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"state": state}))


def load_prev_apmem_state() -> str:
    if not STATE_FILE.exists():
        return "UNKNOWN"
    try:
        return json.loads(STATE_FILE.read_text()).get("state", "UNKNOWN")
    except:
        return "UNKNOWN"
def long_regime_boost(score: int) -> float:
    """
    Convert 0..100 regime score into a modest boost/penalty for long setups.
    Output range is roughly -0.20 .. +0.20
    """
    return max(-0.20, min(0.20, (score - 50) / 250.0))


def short_regime_boost(score: int) -> float:
    """
    Inverse of long_regime_boost for short setups.
    """
    return -long_regime_boost(score)


def position_size_multiplier(score: int) -> float:
    """
    Use smaller size in mixed/chop regime, larger size in strong regimes.
    """
    if score >= 70 or score <= 30:
        return 1.15
    if score >= 55 or score <= 45:
        return 1.00
    return 0.80


def regime_note_for_symbol(score: int, direction: str) -> str:
    direction = direction.upper()
    if direction == "LONG":
        if score >= 70:
            return "Strong long tailwind"
        if score >= 55:
            return "Long tailwind"
        if score <= 30:
            return "Long headwind"
        if score <= 45:
            return "Mild long headwind"
        return "Mixed regime"
    else:
        if score <= 30:
            return "Strong short tailwind"
        if score <= 45:
            return "Short tailwind"
        if score >= 70:
            return "Short headwind"
        if score >= 55:
            return "Mild short headwind"
        return "Mixed regime"