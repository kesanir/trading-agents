from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Dict, List

import numpy as np
import pandas as pd
import yfinance as yf


@dataclass
class ParamSet:
    rv_window: int
    delta: float
    omega: float
    alpha: float
    beta: float
    downside_mult: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rv_window": self.rv_window,
            "delta": self.delta,
            "omega": self.omega,
            "alpha": self.alpha,
            "beta": self.beta,
            "downside_mult": self.downside_mult,
        }


def download_qqq(start: str = "2015-01-01") -> pd.DataFrame:
    df = yf.download("QQQ", start=start, interval="1d", progress=False, auto_adjust=False)

    if df.empty:
        raise ValueError("No QQQ data downloaded")
    #price_df = download_qqq(start="2015-01-01")
    #print(price_df.head())
    #print(price_df.columns)
    
    # Handle MultiIndex columns from newer yfinance versions
    if isinstance(df.columns, pd.MultiIndex):
        if ("Close", "QQQ") in df.columns:
            close = df[("Close", "QQQ")].copy()
        elif ("Adj Close", "QQQ") in df.columns:
            close = df[("Adj Close", "QQQ")].copy()
        else:
            # fallback: first column level named Close
            close_cols = [c for c in df.columns if c[0].lower() == "close"]
            if not close_cols:
                raise ValueError(f"Could not find Close column in MultiIndex columns: {df.columns}")
            close = df[close_cols[0]].copy()
        out = pd.DataFrame({"close": close})
        return out.dropna()

    # Handle normal single-level columns
    cols_lower = {str(c).lower(): c for c in df.columns}

    if "close" in cols_lower:
        close = df[cols_lower["close"]].copy()
    elif "adj close" in cols_lower:
        close = df[cols_lower["adj close"]].copy()
    else:
        raise ValueError(f"Could not find close column. Columns are: {list(df.columns)}")

    out = pd.DataFrame({"close": close})
    return out.dropna()


def compute_apmem_series(
    close: pd.Series,
    rv_window: int,
    delta: float,
    omega: float,
    alpha: float,
    beta: float,
    downside_mult: float,
) -> pd.DataFrame:
    df = pd.DataFrame(index=close.index)
    df["close"] = close.astype(float)
    df["ret"] = df["close"].pct_change()
    df["rv"] = df["ret"].rolling(rv_window).std() * np.sqrt(252)

    df = df.dropna(subset=["ret", "rv"]).copy()
    if df.empty:
        raise ValueError("APMEM input is empty after rolling window")

    df["rv"] = df["rv"].clip(lower=1e-8)
    df["rv_power"] = df["rv"] ** delta

    mu_vals: List[float] = []
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


def rolling_trend(series: pd.Series, short_window: int = 5, long_window: int = 10) -> pd.Series:
    short_avg = series.rolling(short_window).mean()
    long_avg = series.rolling(long_window).mean()
    prev_long = long_avg.shift(short_window)
    return short_avg - prev_long


def rolling_accel(series: pd.Series) -> pd.Series:
    t1 = series.rolling(5).mean()
    t2 = t1.shift(5)
    t3 = t1.shift(10)
    return (t1 - t2) - (t2 - t3)


def classify_states(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["apmem_trend"] = rolling_trend(out["apmem_vol"])
    out["apmem_accel"] = rolling_accel(out["apmem_vol"])
    out["apmem_vol_5d_avg"] = out["apmem_vol"].rolling(5).mean()
    out["shock_ratio"] = out["apmem_vol"] / out["apmem_vol_5d_avg"]

    states = []
    size_mult = []

    for _, row in out.iterrows():
        vol = row["apmem_vol"]
        trend = row["apmem_trend"]
        accel = row["apmem_accel"]
        shock_ratio = row["shock_ratio"]

        state = "NEUTRAL"
        mult = 0.80

        if pd.isna(trend) or pd.isna(accel) or pd.isna(shock_ratio):
            states.append(state)
            size_mult.append(mult)
            continue

        if vol >= 0.30 and trend > 0.004:
            state = "PANIC"
            mult = 0.40
        elif vol >= 0.22 and (trend > 0.002 or accel > 0.002 or shock_ratio > 1.20):
            state = "VOL_EXPANSION"
            mult = 0.60
        elif vol >= 0.18 and trend < -0.002:
            state = "STABILIZING"
            mult = 0.85
        elif vol <= 0.20 and trend < -0.003 and accel < -0.0015:
            state = "RECOVERY"
            mult = 1.20
        elif vol <= 0.14 and trend <= 0.001:
            state = "CALM"
            mult = 1.00

        states.append(state)
        size_mult.append(mult)

    out["state"] = states
    out["size_mult"] = size_mult
    return out


def add_forward_returns(df: pd.DataFrame, horizons: List[int]) -> pd.DataFrame:
    out = df.copy()
    for h in horizons:
        out[f"fwd_ret_{h}d"] = out["close"].shift(-h) / out["close"] - 1.0
    return out


def score_states(df: pd.DataFrame, horizons: List[int]) -> Dict[str, Any]:
    score = 0.0
    details: Dict[str, Any] = {}

    state_order = ["PANIC", "VOL_EXPANSION", "STABILIZING", "RECOVERY", "CALM"]
    counts = df["state"].value_counts().to_dict()

    for state in state_order:
        details[f"count_{state.lower()}"] = int(counts.get(state, 0))

    # Want:
    # RECOVERY => strong forward returns
    # PANIC / VOL_EXPANSION => weak or negative forward returns
    # STABILIZING => improving but not as strong as RECOVERY

    for h in horizons:
        state_means = df.groupby("state")[f"fwd_ret_{h}d"].mean().to_dict()

        rec = float(state_means.get("RECOVERY", 0.0))
        pan = float(state_means.get("PANIC", 0.0))
        exp = float(state_means.get("VOL_EXPANSION", 0.0))
        stab = float(state_means.get("STABILIZING", 0.0))
        calm = float(state_means.get("CALM", 0.0))

        # Core ranking preference:
        # recovery > stabilizing >= calm > expansion > panic
        rank_score = (
            3.0 * rec
            + 1.5 * stab
            + 0.5 * calm
            - 1.5 * exp
            - 2.0 * pan
        )

        # Separation bonus
        separation = (rec - pan) + (stab - exp)

        # Hit rate bonus
        rec_hit = float((df.loc[df["state"] == "RECOVERY", f"fwd_ret_{h}d"] > 0).mean()) if (df["state"] == "RECOVERY").any() else 0.0
        pan_hit = float((df.loc[df["state"] == "PANIC", f"fwd_ret_{h}d"] < 0).mean()) if (df["state"] == "PANIC").any() else 0.0

        horizon_score = rank_score + 0.5 * separation + 0.1 * rec_hit + 0.1 * pan_hit
        score += horizon_score

        details[f"recovery_mean_{h}d"] = round(rec, 4)
        details[f"panic_mean_{h}d"] = round(pan, 4)
        details[f"expansion_mean_{h}d"] = round(exp, 4)
        details[f"stabilizing_mean_{h}d"] = round(stab, 4)
        details[f"calm_mean_{h}d"] = round(calm, 4)
        details[f"recovery_hit_{h}d"] = round(rec_hit, 4)
        details[f"panic_hit_{h}d"] = round(pan_hit, 4)

    # Penalize overly rare states
    n = len(df)
    if n > 0:
        rec_frac = counts.get("RECOVERY", 0) / n
        pan_frac = counts.get("PANIC", 0) / n

        if rec_frac < 0.02:
            score -= 0.25
        if pan_frac < 0.01:
            score -= 0.10

    details["objective_score"] = round(score, 6)
    return details


def run_state_strategy(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Simple regime strategy:
    - LONG during RECOVERY
    - HALF LONG during STABILIZING / CALM
    - FLAT during NEUTRAL
    - FLAT during VOL_EXPANSION
    - SHORT during PANIC
    """
    out = df.copy()
    pos_map = {
        "RECOVERY": 1.0,
        "STABILIZING": 0.5,
        "CALM": 0.5,
        "NEUTRAL": 0.0,
        "VOL_EXPANSION": 0.0,
        "PANIC": -0.5,
    }
    out["position"] = out["state"].map(pos_map).fillna(0.0)
    out["strategy_ret"] = out["position"].shift(1).fillna(0.0) * out["ret"]
    out["equity"] = (1.0 + out["strategy_ret"]).cumprod()
    out["buy_hold"] = (1.0 + out["ret"].fillna(0.0)).cumprod()

    ann_ret = float(out["strategy_ret"].mean() * 252)
    ann_vol = float(out["strategy_ret"].std() * np.sqrt(252))
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    max_dd = float((out["equity"] / out["equity"].cummax() - 1.0).min())

    return {
        "strategy_ann_return": round(ann_ret, 4),
        "strategy_ann_vol": round(ann_vol, 4),
        "strategy_sharpe": round(sharpe, 4),
        "strategy_max_dd": round(max_dd, 4),
        "final_equity": round(float(out["equity"].iloc[-1]), 4),
        "buy_hold_final_equity": round(float(out["buy_hold"].iloc[-1]), 4),
    }


def evaluate_param_set(price_df: pd.DataFrame, params: ParamSet, horizons: List[int]) -> Dict[str, Any]:
    ap = compute_apmem_series(
        close=price_df["close"],
        rv_window=params.rv_window,
        delta=params.delta,
        omega=params.omega,
        alpha=params.alpha,
        beta=params.beta,
        downside_mult=params.downside_mult,
    )
    ap = classify_states(ap)
    ap = add_forward_returns(ap, horizons)

    state_stats = score_states(ap.dropna(), horizons)
    strat_stats = run_state_strategy(ap.dropna())

    result = {
        **params.to_dict(),
        **state_stats,
        **strat_stats,
    }

    # Combined rank: prioritize state separation + practical strategy quality
    result["combined_score"] = round(
        float(state_stats["objective_score"])
        + 0.50 * float(strat_stats["strategy_sharpe"])
        + 0.10 * float(strat_stats["final_equity"] - 1.0),
        6
    )
    return result


def optimize_apmem_on_qqq() -> pd.DataFrame:
    price_df = download_qqq(start="2015-01-01")
    horizons = [3, 5, 10]

    grid = {
        "rv_window": [6, 8, 10, 12, 14],
        "delta": [1.0, 1.1, 1.2, 1.3],
        "omega": [0.01, 0.015, 0.02],
        "alpha": [0.20, 0.25, 0.30, 0.35],
        "beta": [0.50, 0.58, 0.62, 0.68],
        "downside_mult": [1.5, 1.8, 2.0],
    }

    results: List[Dict[str, Any]] = []

    for rv_window, delta, omega, alpha, beta, downside_mult in itertools.product(
        grid["rv_window"],
        grid["delta"],
        grid["omega"],
        grid["alpha"],
        grid["beta"],
        grid["downside_mult"],
    ):
        # Stability guard
        if alpha + beta > 0.98:
            continue
        if alpha + beta < 0.70:
            continue

        params = ParamSet(
            rv_window=rv_window,
            delta=delta,
            omega=omega,
            alpha=alpha,
            beta=beta,
            downside_mult=downside_mult,
        )

        try:
            row = evaluate_param_set(price_df, params, horizons)
            results.append(row)
        except Exception as e:
            print(f"Skip {params}: {e}")

        out = pd.DataFrame(results)

        if out.empty:
            raise ValueError("Optimization produced no valid results. Check data download / close column handling.")

        if "combined_score" not in out.columns:
            raise ValueError(f"'combined_score' missing. Available columns: {list(out.columns)}")

        out = out.sort_values("combined_score", ascending=False).reset_index(drop=True)
        return out

if __name__ == "__main__":
    df = optimize_apmem_on_qqq()

    cols = [
        "combined_score",
        "rv_window",
        "delta",
        "omega",
        "alpha",
        "beta",
        "downside_mult",
        "objective_score",
        "strategy_sharpe",
        "strategy_ann_return",
        "strategy_max_dd",
        "final_equity",
        "recovery_mean_5d",
        "panic_mean_5d",
        "count_recovery",
        "count_panic",
    ]

    print("\nTop 20 parameter sets:\n")
    print(df[cols].head(20).to_string(index=False))

    df.to_csv("qqq_apmem_optimization_results.csv", index=False)
    print("\nSaved: qqq_apmem_optimization_results.csv")