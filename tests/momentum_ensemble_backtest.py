"""
Walk-forward backtest for momentum_ensemble.py.

Replays the three pick streams (momentum / ml / ensemble) over a historical
window and scores picks against realized 1d/5d/21d forward returns from
yfinance daily bars.

Metrics mirror momentum_ensemble_perf.py (hit rate, avg return, sharpe_5d)
so backtest numbers are directly comparable with the forward-walking tracker.

Walk-forward design:
  - Build feature panel once over full history (cross-sectional z-score is
    not lookahead — it uses only same-date rows).
  - For each scan date D, train XGBoost on panel rows where Fwd_Return_21d
    is fully observed using only data <= D (i.e., panel.index <= D - 21d).
  - Retrain every --retrain-every trading days to amortize cost.
  - Scan every --scan-every trading days.
  - LSTM is skipped by default (per-ticker models are too slow for walk-
    forward); Ensemble_Score collapses to normalized XGB, Confidence is
    pinned above min_confidence so the ML stream filter still passes.

Usage:
  python tests/momentum_ensemble_backtest.py
  python tests/momentum_ensemble_backtest.py --years-data 5 --train-years 3 \
      --scan-every 5 --retrain-every 21
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import numpy as np
import pandas as pd

np.random.seed(42)

from tests.momentum_ensemble import (  # noqa: E402
    Config,
    TICKERS,
    compute_momentum_score,
    pick_ensemble_stream,
    pick_ml_stream,
    pick_momentum_stream,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
BACKTEST_OUT = TESTS_DIR / "backtest_results"

STREAMS = ("momentum", "ml", "ensemble")

# Backtest uses a richer feature set than the live scanner: adds
# relative-strength-vs-QQQ, 60d momentum, and distance-from-52w-high.
# These are standard momentum features documented in the literature
# (residual momentum; George & Hwang 2004 for 52w high).
BT_FEATURE_COLS = [
    "ROC_20", "ROC_60",
    "ATR", "RSI",
    "Volume_Ratio", "VIX_Change",
    "RS_QQQ_60", "Dist_52W_High",
    # Lim et al. 2019 additions
    "r6m_vn", "r1y_vn",
    "macd_4_12", "macd_8_24", "macd_32_96",
]


def _baz_macd(price: pd.Series, short: int, long: int) -> pd.Series:
    """Baz et al. 2015 multi-scale MACD, per Lim et al. 2019.

    Squashed normalized MACD = y * exp(-y^2/4) / 0.89 where
    y = [EWM_short(p) - EWM_long(p)] / std_63(p), then normalized again
    by std_252(y). Saturating nonlinearity caps extreme values.
    """
    ewm_s = price.ewm(halflife=short, adjust=False).mean()
    ewm_l = price.ewm(halflife=long, adjust=False).mean()
    m = ewm_s - ewm_l
    std_63 = price.rolling(63).std()
    q = m / std_63
    q_std = q.rolling(252).std()
    y = q / q_std
    return y * np.exp(-(y ** 2) / 4.0) / 0.89


@dataclass
class BacktestConfig:
    years_data: int = 5
    train_years: int = 3
    retrain_every: int = 21
    scan_every: int = 5
    top_n: int = 10
    min_confidence: float = 0.50
    min_momentum_score: int = 60
    horizons: Tuple[int, ...] = (1, 5, 21)


# ---------------------------------------------------------------------------
# Panel builder (keeps rows with NaN Fwd_Return_21d so we can scan recent dates)
# ---------------------------------------------------------------------------

def build_backtest_panel(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    vix: pd.Series,
    regime_series: pd.Series,
) -> pd.DataFrame:
    qqq_close = close["QQQ"] if "QQQ" in close.columns else None
    qqq_roc60 = qqq_close.pct_change(60) if qqq_close is not None else None

    frames = []
    for t in TICKERS:
        if t not in close.columns:
            continue
        df = pd.DataFrame(index=close.index)
        df["Ticker"] = t
        df["Close"] = close[t]
        df["Returns"] = close[t].pct_change()
        df["ROC_5"] = close[t].pct_change(5)
        df["ROC_20"] = close[t].pct_change(20)
        df["ROC_60"] = close[t].pct_change(60)

        tr = pd.concat(
            [
                high[t] - low[t],
                (high[t] - close[t].shift()).abs(),
                (low[t] - close[t].shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["ATR"] = tr.rolling(14).mean() / close[t]

        delta = close[t].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-10)
        df["RSI"] = 100 - (100 / (1 + rs))

        df["Volume_MA"] = volume[t].rolling(20).mean()
        df["Volume_Ratio"] = volume[t] / df["Volume_MA"]

        df["VIX"] = vix
        df["VIX_Change"] = vix.pct_change(10)
        df["Regime"] = regime_series

        # Relative strength vs QQQ over 60d (residual momentum)
        if qqq_roc60 is not None:
            df["RS_QQQ_60"] = df["ROC_60"] - qqq_roc60
        else:
            df["RS_QQQ_60"] = 0.0

        # Distance from 52-week high (higher = closer to high = better)
        rolling_max = close[t].rolling(252).max()
        df["Dist_52W_High"] = close[t] / rolling_max - 1.0

        # Lim et al. 2019: vol-normalized multi-horizon returns
        daily_ret = close[t].pct_change()
        vol_63 = daily_ret.rolling(63).std()
        df["r6m_vn"] = close[t].pct_change(126) / (vol_63 * np.sqrt(126))
        df["r1y_vn"] = close[t].pct_change(252) / (vol_63 * np.sqrt(252))

        # Baz et al. 2015 multi-scale MACD trio
        df["macd_4_12"] = _baz_macd(close[t], 4, 12)
        df["macd_8_24"] = _baz_macd(close[t], 8, 24)
        df["macd_32_96"] = _baz_macd(close[t], 32, 96)

        df["Fwd_Return_21d"] = close[t].pct_change(21).shift(-21)
        df["Trail_50d"] = close[t].pct_change(50)
        df["Target_Signed"] = df["Fwd_Return_21d"] * np.sign(df["Trail_50d"])
        frames.append(df)

    panel = pd.concat(frames)
    panel = panel.dropna(subset=BT_FEATURE_COLS + ["Ticker", "Close", "Trail_50d"])

    def zscore(x):
        std = x.std()
        return (x - x.mean()) / (std if std != 0 else 1)

    panel[BT_FEATURE_COLS] = (
        panel.groupby(level=0)[BT_FEATURE_COLS].transform(zscore).clip(-3, 3)
    )
    panel = panel.dropna(subset=BT_FEATURE_COLS)
    return panel


# ---------------------------------------------------------------------------
# Training / scanning
# ---------------------------------------------------------------------------

def train_xgb(train_panel: pd.DataFrame):
    import xgboost as xgb

    X = train_panel[BT_FEATURE_COLS]
    y = train_panel["Target_Signed"]
    model = xgb.XGBRegressor(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbosity=0,
        n_jobs=1,
        tree_method="hist",
    )
    model.fit(X, y)
    return model


def run_scan(
    scan_date: pd.Timestamp,
    panel: pd.DataFrame,
    xgb_model,
    close: pd.DataFrame,
    volume: pd.DataFrame,
    base_cfg: Config,
) -> Dict[str, pd.DataFrame]:
    if scan_date not in panel.index:
        return {}
    today = panel.loc[[scan_date]].copy()
    if today.empty:
        return {}

    today["XGB_Score"] = xgb_model.predict(today[BT_FEATURE_COLS])

    rng = float(today["XGB_Score"].max() - today["XGB_Score"].min())
    if rng == 0:
        today["Ensemble_Score"] = 0.5
    else:
        today["Ensemble_Score"] = (
            today["XGB_Score"] - today["XGB_Score"].min()
        ) / rng

    today["LSTM_Score"] = 0.0
    today["LSTM_Confidence"] = 0.6
    today["Confidence"] = 0.6

    close_slice = close.loc[:scan_date]
    vol_slice = volume.loc[:scan_date]

    mom_scores: List[float] = []
    volrs: List[float] = []
    rsis: List[float] = []
    closes: List[float] = []
    for t in today["Ticker"]:
        if t not in close_slice.columns or t not in vol_slice.columns:
            mom_scores.append(0.0)
            volrs.append(0.0)
            rsis.append(0.0)
            closes.append(0.0)
            continue
        m = compute_momentum_score(close_slice[t], vol_slice[t])
        mom_scores.append(m["score"])
        volrs.append(m["volume_ratio"])
        rsis.append(m["rsi"])
        px = close_slice[t].dropna()
        closes.append(float(px.iloc[-1]) if not px.empty else 0.0)

    today["Momentum_Score"] = mom_scores
    today["Volume_Ratio_Raw"] = volrs
    today["RSI_Raw"] = rsis
    today["Close_Raw"] = closes

    # ML/ensemble use long-book only: drop downtrending names so signed-target
    # model output translates to "predicted uptrend continuation".
    today_long = today[today["Trail_50d"] > 0].copy()

    return {
        "momentum": pick_momentum_stream(today, base_cfg),
        "ml": pick_ml_stream(today_long, base_cfg),
        "ensemble": pick_ensemble_stream(today_long, base_cfg),
    }


def compute_fwd_returns(
    picks: pd.DataFrame,
    scan_date: pd.Timestamp,
    close: pd.DataFrame,
    horizons: Tuple[int, ...],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for _, row in picks.iterrows():
        ticker = str(row["Ticker"]).upper()
        if ticker not in close.columns:
            continue
        px = close[ticker].dropna()
        if px.empty:
            continue
        entry_i = int(px.index.searchsorted(scan_date))
        if entry_i >= len(px):
            continue
        entry = float(px.iloc[entry_i])
        if entry <= 0:
            continue
        rec: Dict[str, Any] = {
            "scan_date": pd.Timestamp(scan_date).strftime("%Y-%m-%d"),
            "ticker": ticker,
            "entry": round(entry, 4),
        }
        for h in horizons:
            j = entry_i + h
            rec[f"ret_{h}d"] = (
                round((float(px.iloc[j]) - entry) / entry, 5) if j < len(px) else None
            )
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _stream_stats(
    recs: List[Dict[str, Any]], horizons: Tuple[int, ...]
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"n_picks": len(recs)}
    for h in horizons:
        key = f"ret_{h}d"
        rs = [r[key] for r in recs if r.get(key) is not None]
        if not rs:
            stats[f"hit_{h}d"] = None
            stats[f"avg_{h}d"] = None
            continue
        stats[f"hit_{h}d"] = sum(1 for x in rs if x > 0) / len(rs)
        stats[f"avg_{h}d"] = float(np.mean(rs))

    rs5 = [r["ret_5d"] for r in recs if r.get("ret_5d") is not None]
    if len(rs5) >= 2:
        arr = np.array(rs5, dtype=float)
        std = float(arr.std(ddof=1))
        stats["sharpe_5d"] = (
            float(arr.mean()) / std * math.sqrt(252 / 5) if std > 0 else None
        )
    else:
        stats["sharpe_5d"] = None
    return stats


def print_report(
    results: Dict[str, List[Dict[str, Any]]], horizons: Tuple[int, ...]
) -> None:
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS")
    print("=" * 80)

    header = f"{'stream':<10}{'picks':>8}"
    for h in horizons:
        header += f"{'hit_' + str(h) + 'd':>10}{'avg_' + str(h) + 'd':>12}"
    header += f"{'sharpe_5d':>12}"
    print(header)
    print("-" * len(header))

    for stream in STREAMS:
        recs = results.get(stream, [])
        stats = _stream_stats(recs, horizons)
        row = f"{stream:<10}{stats['n_picks']:>8}"
        for h in horizons:
            hit = stats.get(f"hit_{h}d")
            avg = stats.get(f"avg_{h}d")
            row += (
                f"{hit * 100:>9.1f}%" if hit is not None else f"{'n/a':>10}"
            )
            row += (
                f"{avg * 100:>+11.2f}%" if avg is not None else f"{'n/a':>12}"
            )
        sharpe = stats.get("sharpe_5d")
        row += f"{sharpe:>12.2f}" if sharpe is not None else f"{'n/a':>12}"
        print(row)

    print("-" * len(header))
    print("note: 'avg_Nd' is mean realized Nd return per pick, equal-weight")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def backtest(bt_cfg: BacktestConfig, base_cfg: Config) -> None:
    import yfinance as yf

    print("=" * 80)
    print("MOMENTUM ENSEMBLE BACKTEST (walk-forward, XGB-only)")
    print("=" * 80)
    print(
        f"years_data={bt_cfg.years_data} train_years={bt_cfg.train_years} "
        f"retrain_every={bt_cfg.retrain_every}d scan_every={bt_cfg.scan_every}d "
        f"top_n={bt_cfg.top_n}"
    )

    print(f"\nDownloading {bt_cfg.years_data}y data for {len(TICKERS) + 2} symbols...")
    data = yf.download(
        TICKERS + ["QQQ", "^VIX"],
        period=f"{bt_cfg.years_data}y",
        auto_adjust=True,
        progress=False,
    )
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]
    qqq = close["QQQ"]
    vix = close["^VIX"]
    print(
        f"Downloaded {len(close)} days "
        f"[{close.index[0].date()} -> {close.index[-1].date()}]"
    )

    qqq_200 = qqq.rolling(200).mean()
    ma_dist_smooth = ((qqq - qqq_200) / qqq_200).rolling(5).mean()
    regime_series = (ma_dist_smooth > 0).astype(int).shift(1)

    print("\nBuilding feature panel...")
    panel = build_backtest_panel(close, high, low, volume, vix, regime_series)
    print(
        f"Panel: {len(panel)} obs, {panel.index.nunique()} unique dates "
        f"[{panel.index.min().date()} -> {panel.index.max().date()}]"
    )

    all_dates = sorted(panel.index.unique())
    train_buffer_days = bt_cfg.train_years * 252
    max_h = max(bt_cfg.horizons)

    if len(all_dates) < train_buffer_days + max_h + 10:
        print(
            f"Not enough data: need >={train_buffer_days + max_h + 10} "
            f"unique dates, have {len(all_dates)}"
        )
        return

    first_scan_idx = train_buffer_days
    last_scan_idx = len(all_dates) - max_h - 1

    scan_indices = list(range(first_scan_idx, last_scan_idx + 1, bt_cfg.scan_every))
    scan_dates = [all_dates[i] for i in scan_indices]
    retrain_set = set(
        all_dates[i]
        for i in range(first_scan_idx, last_scan_idx + 1, bt_cfg.retrain_every)
    )

    print(
        f"\nBacktest window: {scan_dates[0].date()} -> {scan_dates[-1].date()}  "
        f"({len(scan_dates)} scans, {len(retrain_set)} retrains)"
    )

    results: Dict[str, List[Dict[str, Any]]] = {s: [] for s in STREAMS}
    xgb_model = None
    scans_done = 0
    importance_history: List[np.ndarray] = []

    for scan_date in scan_dates:
        if xgb_model is None or scan_date in retrain_set:
            si = all_dates.index(scan_date)
            cutoff = all_dates[max(0, si - 21)]
            train_panel = panel[
                (panel.index <= cutoff) & panel["Target_Signed"].notna()
            ]
            if len(train_panel) < 1000:
                print(
                    f"[retrain {scan_date.date()}] SKIP (train_rows={len(train_panel)})"
                )
                continue
            xgb_model = train_xgb(train_panel)
            importance_history.append(
                np.asarray(xgb_model.feature_importances_, dtype=float)
            )
            print(
                f"[retrain {scan_date.date()}] train_rows={len(train_panel)}"
            )

        picks_by_stream = run_scan(
            scan_date, panel, xgb_model, close, volume, base_cfg
        )
        if not picks_by_stream:
            continue

        for stream, picks in picks_by_stream.items():
            if picks.empty:
                continue
            recs = compute_fwd_returns(picks, scan_date, close, bt_cfg.horizons)
            results[stream].extend(recs)

        scans_done += 1

    print(f"\nScans executed: {scans_done}")

    BACKTEST_OUT.mkdir(parents=True, exist_ok=True)
    for stream in STREAMS:
        path = BACKTEST_OUT / f"bt_{stream}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results[stream], f, indent=2, default=str)
    print(f"Detailed results written to {BACKTEST_OUT}")

    if importance_history:
        avg_imp = np.mean(np.vstack(importance_history), axis=0)
        imp_rows = sorted(
            zip(BT_FEATURE_COLS, avg_imp), key=lambda x: x[1], reverse=True
        )
        print("\n=== XGBoost feature importance (avg across retrains) ===")
        for name, val in imp_rows:
            bar = "#" * int(val * 200)
            print(f"  {name:<16} {val:>7.4f}  {bar}")

    print_report(results, bt_cfg.horizons)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--years-data", type=int, default=5)
    p.add_argument("--train-years", type=int, default=3)
    p.add_argument("--retrain-every", type=int, default=21)
    p.add_argument("--scan-every", type=int, default=5)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--min-conf", type=float, default=0.50)
    p.add_argument("--min-momentum", type=int, default=60)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    bt_cfg = BacktestConfig(
        years_data=args.years_data,
        train_years=args.train_years,
        retrain_every=args.retrain_every,
        scan_every=args.scan_every,
        top_n=args.top_n,
        min_confidence=args.min_conf,
        min_momentum_score=args.min_momentum,
    )
    base_cfg = Config(
        top_n=args.top_n,
        min_confidence=args.min_conf,
        min_momentum_score=args.min_momentum,
    )
    backtest(bt_cfg, base_cfg)
