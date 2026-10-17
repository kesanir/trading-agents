from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

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

    def key(self) -> Tuple:
        return (
            self.rv_window,
            self.delta,
            self.omega,
            self.alpha,
            self.beta,
            self.downside_mult,
        )


def download_qqq(start: str = "2015-01-01") -> pd.DataFrame:
    df = yf.download("QQQ", start=start, interval="1d", progress=False, auto_adjust=False)

    if df.empty:
        raise ValueError("No QQQ data downloaded")

    if isinstance(df.columns, pd.MultiIndex):
        if ("Close", "QQQ") in df.columns:
            close = df[("Close", "QQQ")].copy()
        elif ("Adj Close", "QQQ") in df.columns:
            close = df[("Adj Close", "QQQ")].copy()
        else:
            close_cols = [c for c in df.columns if str(c[0]).lower() == "close"]
            if not close_cols:
                raise ValueError(f"Could not find Close column in MultiIndex columns: {df.columns}")
            close = df[close_cols[0]].copy()
        return pd.DataFrame({"close": close}).dropna()

    cols_lower = {str(c).lower(): c for c in df.columns}

    if "close" in cols_lower:
        close = df[cols_lower["close"]].copy()
    elif "adj close" in cols_lower:
        close = df[cols_lower["adj close"]].copy()
    else:
        raise ValueError(f"Could not find close column. Columns are: {list(df.columns)}")

    return pd.DataFrame({"close": close}).dropna()


def compute_apmem_series(
    close: pd.Series,
    rv_window: int,
    delta: float,
    omega: float,
    alpha: float,
    beta: float,
    downside_mult: float,
) -> pd.DataFrame:
    # Force 1D Series
    if isinstance(close, pd.DataFrame):
        if close.shape[1] != 1:
            raise ValueError(f"Expected 1 close column, got {close.shape[1]} columns: {list(close.columns)}")
        close = close.iloc[:, 0]

    close = pd.Series(close).astype(float).copy()
    close.name = "close"

    df = pd.DataFrame(index=close.index)
    df["close"] = close.to_numpy()
    df["ret"] = df["close"].pct_change()
    df["rv"] = df["ret"].rolling(rv_window).std() * np.sqrt(252)

    df = df.dropna(subset=["ret", "rv"]).copy()
    if df.empty:
        raise ValueError("Empty series after RV calculation")

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


def rolling_trend(series: pd.Series, short_window: int = 5, long_window: int = 10) -> pd.Series:
    short_avg = series.rolling(short_window).mean()
    prev_avg = series.shift(short_window).rolling(short_window).mean()
    return short_avg - prev_avg


def rolling_accel(series: pd.Series) -> pd.Series:
    t1 = series.rolling(5).mean()
    t2 = t1.shift(5)
    t3 = t1.shift(10)
    return (t1 - t2) - (t2 - t3)


def classify_states(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # Make sure apmem_vol is a plain Series
    if isinstance(out["apmem_vol"], pd.DataFrame):
        out["apmem_vol"] = out["apmem_vol"].iloc[:, 0]

    out["apmem_trend"] = rolling_trend(out["apmem_vol"])
    out["apmem_accel"] = rolling_accel(out["apmem_vol"])
    out["apmem_vol_5d_avg"] = out["apmem_vol"].rolling(5).mean()
    out["shock_ratio"] = out["apmem_vol"] / out["apmem_vol_5d_avg"]

    # Default state
    out["state"] = "NEUTRAL"
    out["size_mult"] = 0.80

    panic_mask = (
        (out["apmem_vol"] >= 0.30) &
        (out["apmem_trend"] > 0.004)
    )

    expansion_mask = (
        (out["apmem_vol"] >= 0.22) &
        (
            (out["apmem_trend"] > 0.002) |
            (out["apmem_accel"] > 0.002) |
            (out["shock_ratio"] > 1.20)
        )
    )

    stabilizing_mask = (
        (out["apmem_vol"] >= 0.18) &
        (out["apmem_trend"] < -0.002)
    )

    recovery_mask = (
        (out["apmem_vol"] <= 0.20) &
        (out["apmem_trend"] < -0.003) &
        (out["apmem_accel"] < -0.0015)
    )

    calm_mask = (
        (out["apmem_vol"] <= 0.14) &
        (out["apmem_trend"] <= 0.001)
    )

    # Apply in priority order
    out.loc[calm_mask, ["state", "size_mult"]] = ["CALM", 1.00]
    out.loc[recovery_mask, ["state", "size_mult"]] = ["RECOVERY", 1.20]
    out.loc[stabilizing_mask, ["state", "size_mult"]] = ["STABILIZING", 0.85]
    out.loc[expansion_mask, ["state", "size_mult"]] = ["VOL_EXPANSION", 0.60]
    out.loc[panic_mask, ["state", "size_mult"]] = ["PANIC", 0.40]

    return out

def add_forward_returns(df: pd.DataFrame, horizons: List[int]) -> pd.DataFrame:
    out = df.copy()
    for h in horizons:
        out[f"fwd_ret_{h}d"] = out["close"].shift(-h) / out["close"] - 1.0
    return out


def score_states(df: pd.DataFrame, horizons: List[int]) -> Dict[str, Any]:
    score = 0.0
    details: Dict[str, Any] = {}
    counts = df["state"].value_counts().to_dict()

    for h in horizons:
        state_means = df.groupby("state")[f"fwd_ret_{h}d"].mean().to_dict()

        rec = float(state_means.get("RECOVERY", 0.0))
        pan = float(state_means.get("PANIC", 0.0))
        exp = float(state_means.get("VOL_EXPANSION", 0.0))
        stab = float(state_means.get("STABILIZING", 0.0))
        calm = float(state_means.get("CALM", 0.0))

        rank_score = (
            3.0 * rec
            + 1.5 * stab
            + 0.5 * calm
            - 1.5 * exp
            - 2.0 * pan
        )
        separation = (rec - pan) + (stab - exp)

        rec_hit = float((df.loc[df["state"] == "RECOVERY", f"fwd_ret_{h}d"] > 0).mean()) if (df["state"] == "RECOVERY").any() else 0.0
        pan_hit = float((df.loc[df["state"] == "PANIC", f"fwd_ret_{h}d"] < 0).mean()) if (df["state"] == "PANIC").any() else 0.0

        horizon_score = rank_score + 0.5 * separation + 0.1 * rec_hit + 0.1 * pan_hit
        score += horizon_score

        details[f"recovery_mean_{h}d"] = round(rec, 4)
        details[f"panic_mean_{h}d"] = round(pan, 4)
        details[f"recovery_hit_{h}d"] = round(rec_hit, 4)
        details[f"panic_hit_{h}d"] = round(pan_hit, 4)

    n = len(df)
    if n > 0:
        rec_frac = counts.get("RECOVERY", 0) / n
        pan_frac = counts.get("PANIC", 0) / n
        if rec_frac < 0.02:
            score -= 0.25
        if pan_frac < 0.01:
            score -= 0.10

    details["objective_score"] = round(score, 6)
    details["count_recovery"] = int(counts.get("RECOVERY", 0))
    details["count_panic"] = int(counts.get("PANIC", 0))
    return details


def run_state_strategy(df: pd.DataFrame) -> Dict[str, Any]:
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
    out["equity"] = (1.0 + out["strategy_ret"].fillna(0.0)).cumprod()

    ann_ret = float(out["strategy_ret"].mean() * 252)
    ann_vol = float(out["strategy_ret"].std() * np.sqrt(252))
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    max_dd = float((out["equity"] / out["equity"].cummax() - 1.0).min())
    final_equity = float(out["equity"].iloc[-1]) if not out.empty else 1.0

    return {
        "strategy_ann_return": round(ann_ret, 4),
        "strategy_ann_vol": round(ann_vol, 4),
        "strategy_sharpe": round(sharpe, 4),
        "strategy_max_dd": round(max_dd, 4),
        "final_equity": round(final_equity, 4),
    }


def evaluate_param_set(price_df: pd.DataFrame, params: ParamSet, horizons: List[int]) -> Dict[str, Any]:
    if "close" not in price_df.columns:
        raise ValueError(f"price_df missing close column: {list(price_df.columns)}")
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

    clean = ap.dropna().copy()
    if clean.empty:
        raise ValueError("No clean rows after state/forward-return construction")

    state_stats = score_states(clean, horizons)
    strat_stats = run_state_strategy(clean)

    result = {
        **params.to_dict(),
        **state_stats,
        **strat_stats,
    }
    result["combined_score"] = round(
        float(state_stats["objective_score"])
        + 0.50 * float(strat_stats["strategy_sharpe"])
        + 0.10 * float(strat_stats["final_equity"] - 1.0),
        6
    )
    return result


def build_param_grid() -> List[ParamSet]:
    grid = {
        "rv_window": [8, 10],
        "delta": [1.1, 1.2],
        "omega": [0.01, 0.02],
        "alpha": [0.25, 0.30],
        "beta": [0.58, 0.62],
        "downside_mult": [1.8, 2.0],
    }

    params: List[ParamSet] = []
    for rv_window, delta, omega, alpha, beta, downside_mult in itertools.product(
        grid["rv_window"],
        grid["delta"],
        grid["omega"],
        grid["alpha"],
        grid["beta"],
        grid["downside_mult"],
    ):
        if alpha + beta > 0.98:
            continue
        if alpha + beta < 0.70:
            continue

        params.append(
            ParamSet(
                rv_window=rv_window,
                delta=delta,
                omega=omega,
                alpha=alpha,
                beta=beta,
                downside_mult=downside_mult,
            )
        )
    return params


def generate_walkforward_windows(
    df: pd.DataFrame,
    train_years: int = 5,
    test_months: int = 6,
) -> List[Dict[str, Any]]:
    dates = pd.Index(df.index).sort_values().unique()
    if len(dates) < 252 * train_years:
        raise ValueError("Not enough history for requested walk-forward window")

    windows: List[Dict[str, Any]] = []
    start_idx = 0
    step = 21 * test_months  # approximate trading days

    while True:
        train_start = dates[start_idx]
        train_end_idx = start_idx + 252 * train_years
        test_end_idx = train_end_idx + step

        if test_end_idx >= len(dates):
            break

        train_end = dates[train_end_idx]
        test_start = dates[train_end_idx + 1]
        test_end = dates[test_end_idx]

        windows.append(
            {
                "train_start": train_start,
                "train_end": train_end,
                "test_start": test_start,
                "test_end": test_end,
            }
        )
        start_idx += step

    return windows


def slice_df(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df.loc[(df.index >= start) & (df.index <= end)].copy()


def walkforward_optimize_qqq(
    train_years: int = 5,
    test_months: int = 6,
    top_k: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    price_df = download_qqq(start="2015-01-01")
    horizons = [3, 5, 10]
    param_grid = build_param_grid()
    windows = generate_walkforward_windows(price_df, train_years=train_years, test_months=test_months)

    wf_rows: List[Dict[str, Any]] = []
    param_oos_rows: List[Dict[str, Any]] = []

    for i, w in enumerate(windows, start=1):
        train_df = slice_df(price_df, w["train_start"], w["train_end"])
        test_df = slice_df(price_df, w["test_start"], w["test_end"])

        train_results: List[Dict[str, Any]] = []

        for params in param_grid:
            try:
                res = evaluate_param_set(train_df, params, horizons)
                train_results.append(res)
            except Exception:
                continue

        if not train_results:
            continue

        train_ranked = pd.DataFrame(train_results).sort_values("combined_score", ascending=False).reset_index(drop=True)
        winners = train_ranked.head(top_k).copy()

        # Evaluate top-k on the next out-of-sample segment
        for _, row in winners.iterrows():
            params = ParamSet(
                rv_window=int(row["rv_window"]),
                delta=float(row["delta"]),
                omega=float(row["omega"]),
                alpha=float(row["alpha"]),
                beta=float(row["beta"]),
                downside_mult=float(row["downside_mult"]),
            )

            try:
                oos = evaluate_param_set(test_df, params, horizons)
            except Exception:
                continue

            wf_rows.append(
                {
                    "window_id": i,
                    "train_start": str(w["train_start"].date()),
                    "train_end": str(w["train_end"].date()),
                    "test_start": str(w["test_start"].date()),
                    "test_end": str(w["test_end"].date()),
                    "rank_in_train": int((_ + 1)),
                    "train_combined_score": float(row["combined_score"]),
                    "oos_combined_score": float(oos["combined_score"]),
                    "oos_strategy_sharpe": float(oos["strategy_sharpe"]),
                    "oos_strategy_ann_return": float(oos["strategy_ann_return"]),
                    "oos_strategy_max_dd": float(oos["strategy_max_dd"]),
                    "oos_recovery_mean_5d": float(oos.get("recovery_mean_5d", 0.0)),
                    "oos_panic_mean_5d": float(oos.get("panic_mean_5d", 0.0)),
                    **params.to_dict(),
                }
            )

        # Track best in-sample winner separately
        best = winners.iloc[0]
        param_oos_rows.append(
            {
                "window_id": i,
                "train_start": str(w["train_start"].date()),
                "train_end": str(w["train_end"].date()),
                "test_start": str(w["test_start"].date()),
                "test_end": str(w["test_end"].date()),
                "best_train_rv_window": int(best["rv_window"]),
                "best_train_delta": float(best["delta"]),
                "best_train_omega": float(best["omega"]),
                "best_train_alpha": float(best["alpha"]),
                "best_train_beta": float(best["beta"]),
                "best_train_downside_mult": float(best["downside_mult"]),
                "best_train_score": float(best["combined_score"]),
            }
        )

    wf_df = pd.DataFrame(wf_rows)
    if wf_df.empty:
        raise ValueError("Walk-forward produced no out-of-sample results")

    summary = (
        wf_df.groupby(["rv_window", "delta", "omega", "alpha", "beta", "downside_mult"], as_index=False)
        .agg(
            mean_oos_score=("oos_combined_score", "mean"),
            median_oos_score=("oos_combined_score", "median"),
            mean_oos_sharpe=("oos_strategy_sharpe", "mean"),
            mean_oos_return=("oos_strategy_ann_return", "mean"),
            mean_oos_max_dd=("oos_strategy_max_dd", "mean"),
            mean_oos_recovery_5d=("oos_recovery_mean_5d", "mean"),
            mean_oos_panic_5d=("oos_panic_mean_5d", "mean"),
            appearances=("window_id", "count"),
        )
        .sort_values(["mean_oos_score", "mean_oos_sharpe"], ascending=False)
        .reset_index(drop=True)
    )

    return wf_df, summary


if __name__ == "__main__":
    wf_df, summary = walkforward_optimize_qqq(
        train_years=5,
        test_months=6,
        top_k=10,
    )

    print("\nTop walk-forward robust parameter sets:\n")
    cols = [
        "mean_oos_score",
        "median_oos_score",
        "mean_oos_sharpe",
        "mean_oos_return",
        "mean_oos_max_dd",
        "mean_oos_recovery_5d",
        "mean_oos_panic_5d",
        "appearances",
        "rv_window",
        "delta",
        "omega",
        "alpha",
        "beta",
        "downside_mult",
    ]
    print(summary[cols].head(20).to_string(index=False))

    wf_df.to_csv("qqq_apmem_walkforward_detail.csv", index=False)
    summary.to_csv("qqq_apmem_walkforward_summary.csv", index=False)

    print("\nSaved:")
    print("- qqq_apmem_walkforward_detail.csv")
    print("- qqq_apmem_walkforward_summary.csv")