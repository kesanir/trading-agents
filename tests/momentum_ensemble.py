"""
Integrated momentum scanner:
  - momentumscan rule score (pure momentum stream)
  - xgboost + lstm ensemble (pure ml stream)
  - both together (ensemble-confirmed stream)

Emits three independent trade intent lists ready for
app.services.auto_trade_executor.execute_trade(...).

Independent from daily_runner.py / state_change_runner.py.
Writes pick ledgers to app/performance/momentum_scanner/ so the
existing QQQ intraday signal_outcome_service never sees them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import numpy as np
import pandas as pd

np.random.seed(42)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR = PROJECT_ROOT / "app" / "performance" / "momentum_scanner"
TESTS_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    train_years: int = 4
    lookback_lstm: int = 30
    lookback_xgboost: int = 21
    ensemble_weight_xgb: float = 0.6
    ensemble_weight_lstm: float = 0.4
    top_n: int = 5
    min_confidence: float = 0.50
    min_momentum_score: int = 60
    test_days: int = 60
    default_dte: int = 7
    default_delta: float = 0.35
    default_width: float = 5.0


TICKERS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO",
    "COST", "NFLX", "ADBE", "PEP", "CSCO", "TMUS", "AMD", "INTC",
    "INTU", "QCOM", "TXN", "AMGN", "HON", "AMAT", "SBUX",
    "ISRG", "BKNG", "GILD", "ADI", "ADP", "VRTX", "MDLZ", "LRCX",
    "REGN", "MU", "PANW", "PYPL", "SNPS", "CDNS", "KLAC", "ASML",
    "MELI", "CRWD", "ABNB", "FTNT", "WDAY", "MRNA", "CTAS", "DXCM",
    "AEP", "NXPI", "CHTR", "MAR", "ADSK", "MCHP", "KDP",
    "MNST", "EXC", "CSX", "CPRT",
    "DDOG", "FAST", "BKR", "TTD", "CTSH",
    "EA", "GEHC", "VRSK", "ON", "ZS", "S",
    "TEAM", "ILMN", "DASH",
    "GFS", "WBD", "MRVL", "TTWO", "ZM", "ENPH",
]

FEATURE_COLS = [
    "ROC_20", "ROC_60",
    "ATR", "RSI",
    "Volume_Ratio", "VIX_Change",
    "RS_QQQ_60", "Dist_52W_High",
    "r6m_vn", "r1y_vn",
    "macd_4_12", "macd_8_24", "macd_32_96",
]


# ---------------------------------------------------------------------------
# Regime
# ---------------------------------------------------------------------------

def get_ma_regime(qqq_close: pd.Series) -> str:
    qqq_200 = qqq_close.rolling(200).mean()
    dist = (qqq_close - qqq_200) / qqq_200
    smooth = dist.rolling(5).mean()
    if smooth.dropna().empty:
        return "NEUTRAL"
    return "BULL" if smooth.iloc[-1] > 0 else "BEAR"


def get_hmm_regime(qqq_close: pd.Series) -> str:
    try:
        from hmmlearn.hmm import GaussianHMM

        returns = np.log(qqq_close).diff().dropna().values.reshape(-1, 1)
        if len(returns) < 100:
            return "NEUTRAL"
        model = GaussianHMM(n_components=3, n_iter=500, random_state=42)
        model.fit(returns)
        state = int(model.predict(returns)[-1])
        means = [float(model.means_[i][0]) for i in range(3)]
        order = sorted(range(3), key=lambda i: means[i])
        labels = {order[0]: "BEAR", order[1]: "NEUTRAL", order[2]: "BULL"}
        return labels[state]
    except Exception:
        return "NEUTRAL"


def combine_regimes(ma: str, hmm: str) -> str:
    if ma == "BULL" and hmm == "BULL":
        return "BULL"
    if ma == "BEAR" or hmm == "BEAR":
        return "BEAR"
    return "NEUTRAL"


# ---------------------------------------------------------------------------
# Momentum rule score (from momentumscan)
# ---------------------------------------------------------------------------

def compute_momentum_score(close_t: pd.Series, volume_t: pd.Series) -> Dict[str, float]:
    close_t = close_t.dropna()
    volume_t = volume_t.dropna()
    if len(close_t) < 60:
        return {"score": 0.0, "volume_ratio": 0.0, "rsi": 0.0}

    delta = close_t.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    rsi = 100 - (100 / (1 + rs))

    sma20 = close_t.rolling(20).mean()
    sma50 = close_t.rolling(50).mean()
    roc10 = close_t.pct_change(10)
    volr = volume_t / volume_t.rolling(20).mean()

    last_close = float(close_t.iloc[-1])
    last_sma20 = float(sma20.iloc[-1])
    last_sma50 = float(sma50.iloc[-1])
    last_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 0.0
    last_roc10 = float(roc10.iloc[-1]) if not pd.isna(roc10.iloc[-1]) else 0.0
    last_volr = float(volr.iloc[-1]) if not pd.isna(volr.iloc[-1]) else 0.0

    score = (
        int(last_close > last_sma20) * 20
        + int(last_sma20 > last_sma50) * 20
        + int(last_rsi > 55) * 20
        + int(last_roc10 > 0) * 20
        + int(last_volr > 1.3) * 20
    )
    return {"score": float(score), "volume_ratio": last_volr, "rsi": last_rsi}


# ---------------------------------------------------------------------------
# XGBoost feature panel
# ---------------------------------------------------------------------------

def _baz_macd(price: pd.Series, short: int, long: int) -> pd.Series:
    ewm_s = price.ewm(halflife=short, adjust=False).mean()
    ewm_l = price.ewm(halflife=long, adjust=False).mean()
    m = ewm_s - ewm_l
    std_63 = price.rolling(63).std()
    q = m / std_63
    q_std = q.rolling(252).std()
    y = q / q_std
    return y * np.exp(-(y ** 2) / 4.0) / 0.89


def build_feature_panel(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    vix: pd.Series,
    regime_series: pd.Series,
    cfg: Config,
) -> pd.DataFrame:
    qqq_close = close["QQQ"]
    qqq_roc60 = qqq_close.pct_change(60)

    frames = []
    for t in TICKERS:
        if t not in close.columns:
            continue
        price = close[t]
        df = pd.DataFrame(index=close.index)
        df["Ticker"] = t
        df["Close"] = price
        df["ROC_20"] = price.pct_change(20)
        df["ROC_60"] = price.pct_change(60)

        tr = pd.concat(
            [
                high[t] - low[t],
                (high[t] - price.shift()).abs(),
                (low[t] - price.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["ATR"] = tr.rolling(14).mean() / price

        delta = price.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-10)
        df["RSI"] = 100 - (100 / (1 + rs))

        df["Volume_MA"] = volume[t].rolling(20).mean()
        df["Volume_Ratio"] = volume[t] / df["Volume_MA"]

        df["VIX_Change"] = vix.pct_change(10)

        df["RS_QQQ_60"] = df["ROC_60"] - qqq_roc60
        df["Dist_52W_High"] = price / price.rolling(252).max() - 1.0

        daily_ret = price.pct_change()
        vol_63 = daily_ret.rolling(63).std()
        df["r6m_vn"] = price.pct_change(126) / (vol_63 * np.sqrt(126))
        df["r1y_vn"] = price.pct_change(252) / (vol_63 * np.sqrt(252))

        df["macd_4_12"] = _baz_macd(price, 4, 12)
        df["macd_8_24"] = _baz_macd(price, 8, 24)
        df["macd_32_96"] = _baz_macd(price, 32, 96)

        df["Trail_50d"] = price.pct_change(50)
        df["Fwd_Return_21d"] = (
            price.pct_change(cfg.lookback_xgboost).shift(-cfg.lookback_xgboost)
        )
        df["Target_Signed"] = df["Fwd_Return_21d"] * np.sign(df["Trail_50d"])
        df["Regime"] = regime_series
        frames.append(df)

    panel = pd.concat(frames)
    # Keep today's row even though Fwd_Return_21d/Target_Signed are NaN for it.
    panel = panel.dropna(subset=FEATURE_COLS + ["Trail_50d"])

    def zscore(x):
        std = x.std()
        return (x - x.mean()) / (std if std != 0 else 1)

    panel[FEATURE_COLS] = (
        panel.groupby(level=0)[FEATURE_COLS].transform(zscore).clip(-3, 3)
    )
    return panel


def train_xgboost(panel: pd.DataFrame, cfg: Config):
    import xgboost as xgb

    train_panel = panel[panel["Target_Signed"].notna()]
    X_train = train_panel[FEATURE_COLS]
    y_train = train_panel["Target_Signed"]

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
    model.fit(X_train, y_train)
    return model


# ---------------------------------------------------------------------------
# LSTM
# ---------------------------------------------------------------------------

def _build_simple_lstm():
    import torch
    import torch.nn as nn

    class SimpleLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(1, 32, batch_first=True, dropout=0.2)
            self.fc = nn.Linear(32, 1)

        def forward(self, x):
            out, _ = self.lstm(x)
            return torch.tanh(self.fc(out[:, -1, :])) * 0.1

    return SimpleLSTM()


def train_lstm_per_ticker(
    close: pd.DataFrame, cfg: Config
) -> Dict[str, Dict[str, float]]:
    import torch
    import torch.nn as nn
    from sklearn.preprocessing import StandardScaler

    torch.manual_seed(42)

    results: Dict[str, Dict[str, float]] = {}
    for ticker in TICKERS:
        if ticker not in close.columns:
            continue
        try:
            series = close[ticker].dropna()
            if len(series) < 200:
                continue

            returns = series.pct_change().dropna().values.reshape(-1, 1)
            scaler = StandardScaler()
            scaled = scaler.fit_transform(returns)

            X, y = [], []
            for i in range(cfg.lookback_lstm, len(scaled)):
                X.append(scaled[i - cfg.lookback_lstm : i])
                y.append(scaled[i])
            if not X:
                continue

            X_t = torch.FloatTensor(np.array(X))
            y_t = torch.FloatTensor(np.array(y))

            split = len(X_t) - cfg.test_days
            if split <= 0:
                continue

            X_train, y_train = X_t[:split], y_t[:split]
            X_test, y_test = X_t[split:], y_t[split:]

            model = _build_simple_lstm()
            criterion = nn.MSELoss()
            optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

            for _ in range(30):
                model.train()
                optimizer.zero_grad()
                loss = criterion(model(X_train), y_train)
                loss.backward()
                optimizer.step()

            model.eval()
            with torch.no_grad():
                pred = model(X_test).numpy()
                actual = y_test.numpy()
            test_acc = float(
                (np.sign(pred.flatten()) == np.sign(actual.flatten())).mean()
            )

            last_seq = torch.FloatTensor(
                scaled[-cfg.lookback_lstm :]
            ).unsqueeze(0)
            with torch.no_grad():
                next_scaled = model(last_seq).item()
            next_pred = float(
                scaler.inverse_transform([[next_scaled]])[0, 0]
            )

            results[ticker] = {"prediction": next_pred, "accuracy": test_acc}
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# Today panel + three pick streams
# ---------------------------------------------------------------------------

def build_today_panel(
    panel: pd.DataFrame,
    xgb_model: Any,
    lstm_results: Dict[str, Dict[str, float]],
    close: pd.DataFrame,
    volume: pd.DataFrame,
    cfg: Config,
) -> pd.DataFrame:
    latest_date = panel.index.max()
    today = panel.loc[latest_date].copy()
    if isinstance(today, pd.Series):
        today = today.to_frame().T

    today["XGB_Score"] = xgb_model.predict(today[FEATURE_COLS])

    lstm_scores, lstm_conf = [], []
    for t in today["Ticker"]:
        if t in lstm_results:
            lstm_scores.append(lstm_results[t]["prediction"])
            lstm_conf.append(lstm_results[t]["accuracy"])
        else:
            lstm_scores.append(0.0)
            lstm_conf.append(0.5)
    today["LSTM_Score"] = lstm_scores
    today["LSTM_Confidence"] = lstm_conf

    def norm(x: pd.Series) -> pd.Series:
        rng = x.max() - x.min()
        if rng == 0:
            return pd.Series(0.5, index=x.index)
        return (x - x.min()) / rng

    xgb_norm = norm(today["XGB_Score"])
    lstm_norm = norm(today["LSTM_Score"])
    today["Ensemble_Score"] = (
        cfg.ensemble_weight_xgb * xgb_norm + cfg.ensemble_weight_lstm * lstm_norm
    )
    today["Confidence"] = today["LSTM_Confidence"]

    mom_scores, volrs, rsis = [], [], []
    for t in today["Ticker"]:
        m = compute_momentum_score(close[t], volume[t])
        mom_scores.append(m["score"])
        volrs.append(m["volume_ratio"])
        rsis.append(m["rsi"])
    today["Momentum_Score"] = mom_scores
    today["Volume_Ratio_Raw"] = volrs
    today["RSI_Raw"] = rsis

    today["Close_Raw"] = [float(close[t].iloc[-1]) for t in today["Ticker"]]
    return today


def pick_momentum_stream(today: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    cand = today[today["Momentum_Score"] >= cfg.min_momentum_score].copy()
    return cand.sort_values(
        ["Momentum_Score", "Volume_Ratio_Raw"], ascending=[False, False]
    ).head(cfg.top_n)


def pick_ml_stream(today: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    cand = today[
        (today["Confidence"] > cfg.min_confidence)
        & (today["Trail_50d"] > 0)
    ].copy()
    return cand.sort_values("Ensemble_Score", ascending=False).head(cfg.top_n)


def pick_ensemble_stream(today: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    cand = today[
        (today["Confidence"] > cfg.min_confidence)
        & (today["Momentum_Score"] >= cfg.min_momentum_score)
        & (today["Trail_50d"] > 0)
    ].copy()
    cand["Combined"] = cand["Ensemble_Score"] * (cand["Momentum_Score"] / 100.0)
    return cand.sort_values("Combined", ascending=False).head(cfg.top_n)


# ---------------------------------------------------------------------------
# Trade intents
# ---------------------------------------------------------------------------

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def build_trade_intents(
    picks: pd.DataFrame, source: str, cfg: Config
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for _, row in picks.iterrows():
        if source == "momentum":
            score = float(row["Momentum_Score"]) / 100.0
            size_mult = _clip(score * 1.10, 0.50, 1.10)
        elif source == "ml":
            score = float(row["Ensemble_Score"])
            size_mult = _clip(score * float(row["Confidence"]) * 1.50, 0.50, 1.20)
        else:  # ensemble
            score = float(row["Ensemble_Score"])
            size_mult = _clip(
                score * (float(row["Momentum_Score"]) / 100.0) * 1.50, 0.50, 1.30
            )

        out.append(
            {
                "ticker": str(row["Ticker"]).upper(),
                "direction": "bullish",
                "strategy": "debit_call",
                "dte": cfg.default_dte,
                "delta": cfg.default_delta,
                "width": cfg.default_width,
                "score": round(float(score), 4),
                "size_mult": round(float(size_mult), 2),
                "source": source,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Ledger writes (decoupled from signals.jsonl)
# ---------------------------------------------------------------------------

def _pick_id(source: str, ticker: str, scan_date: str) -> str:
    return hashlib.sha256(f"{source}|{ticker}|{scan_date}".encode()).hexdigest()[:8]


def append_pick_ledger(
    picks: pd.DataFrame,
    source: str,
    regime_ma: str,
    regime_hmm: str,
    regime_combined: str,
) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    path = LEDGER_DIR / f"{source}_picks.jsonl"
    scan_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    with open(path, "a", encoding="utf-8") as f:
        for _, row in picks.iterrows():
            ticker = str(row["Ticker"]).upper()
            rec = {
                "pick_id": _pick_id(source, ticker, scan_date),
                "source": source,
                "scan_date": scan_date,
                "scanned_at": datetime.now(tz=timezone.utc).isoformat(),
                "ticker": ticker,
                "entry_close": round(float(row["Close_Raw"]), 4),
                "momentum_score": float(row["Momentum_Score"]),
                "confidence": round(float(row["Confidence"]), 4),
                "ensemble_score": round(float(row["Ensemble_Score"]), 4),
                "xgb_score": round(float(row["XGB_Score"]), 6),
                "lstm_score": round(float(row["LSTM_Score"]), 6),
                "volume_ratio": round(float(row["Volume_Ratio_Raw"]), 4),
                "rsi": round(float(row["RSI_Raw"]), 2),
                "regime_combined": regime_combined,
                "regime_ma": regime_ma,
                "regime_hmm": regime_hmm,
            }
            f.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def print_stream(label: str, picks: pd.DataFrame) -> None:
    print(f"\n=== {label} PICKS ({len(picks)}) ===")
    if picks.empty:
        print("  (none)")
        return
    cols = [
        "Ticker", "Close_Raw", "Ensemble_Score", "Confidence",
        "Momentum_Score", "Volume_Ratio_Raw",
    ]
    disp = picks[cols].copy()
    disp.columns = ["Ticker", "Price", "Ensemble", "Conf", "MomScore", "VolR"]
    disp["Price"] = disp["Price"].map(lambda x: f"{x:.2f}")
    disp["Ensemble"] = disp["Ensemble"].map(lambda x: f"{x:.3f}")
    disp["Conf"] = disp["Conf"].map(lambda x: f"{x:.1%}")
    disp["MomScore"] = disp["MomScore"].map(lambda x: f"{int(x)}")
    disp["VolR"] = disp["VolR"].map(lambda x: f"{x:.2f}")
    print(disp.to_string(index=False))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def execute_intents(intents: List[Dict[str, Any]]) -> None:
    if not intents:
        print("[execute] no intents to fire")
        return

    import asyncio

    from alpaca.trading.client import TradingClient
    from core.config import settings

    from app.services.auto_trade_executor import execute_trade
    from app.services.execution_log_service import already_executed, append_log
    from app.services.open_position_guard import has_open_position

    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )
    try:
        account_equity = float(client.get_account().equity)
    except Exception as e:
        print(f"[execute] failed to fetch account equity: {e}")
        return

    print(f"[execute] account_equity={account_equity:.2f}")

    for intent in intents:
        ticker = intent["ticker"]
        direction = intent["direction"]
        source = intent.get("source", "unknown")

        if already_executed(ticker=ticker, direction=direction, lookback_minutes=180):
            print(f"[execute] SKIP {source}/{ticker} — already_executed")
            continue
        if has_open_position(ticker):
            print(f"[execute] SKIP {source}/{ticker} — has_open_position")
            continue

        try:
            result, sizing = asyncio.run(
                execute_trade(intent, account_equity=account_equity)
            )
            success = bool(getattr(result, "success", False))
            order_id = getattr(result, "order_id", None)
            print(
                f"[execute] {source}/{ticker} success={success} "
                f"order_id={order_id} sizing={sizing}"
            )
            append_log(
                {
                    "timestamp": datetime.now().isoformat(),
                    "ticker": ticker,
                    "direction": direction,
                    "source": source,
                    "intent": intent,
                    "sizing": sizing,
                    "success": success,
                    "order_id": order_id,
                }
            )
        except Exception as e:
            print(f"[execute] ERROR {source}/{ticker}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(cfg: Config, execute: Optional[str], force_neutral: bool) -> None:
    print("=" * 80)
    print("MOMENTUM ENSEMBLE SCANNER")
    print("=" * 80)
    print(
        f"Universe: {len(TICKERS)} tickers | top_n={cfg.top_n} | "
        f"min_conf={cfg.min_confidence} | min_mom={cfg.min_momentum_score}"
    )

    import yfinance as yf

    print("\nDownloading data...")
    data = yf.download(
        TICKERS + ["QQQ", "^VIX"],
        period=f"{cfg.train_years}y",
        auto_adjust=True,
        progress=False,
    )
    close = data["Close"]
    high = data["High"]
    low = data["Low"]
    volume = data["Volume"]
    qqq = close["QQQ"]
    vix = close["^VIX"]
    print(f"Downloaded {len(close)} days")

    regime_ma = get_ma_regime(qqq)
    regime_hmm = get_hmm_regime(qqq)
    regime_combined = combine_regimes(regime_ma, regime_hmm)
    print(
        f"Regime: ma={regime_ma} hmm={regime_hmm} combined={regime_combined}"
    )

    qqq_200 = qqq.rolling(200).mean()
    ma_dist_smooth = ((qqq - qqq_200) / qqq_200).rolling(5).mean()
    regime_series = (ma_dist_smooth > 0).astype(int).shift(1)

    print("Building feature panel...")
    panel = build_feature_panel(close, high, low, volume, vix, regime_series, cfg)
    print(f"Panel: {len(panel)} obs")

    print("Training XGBoost...")
    xgb_model = train_xgboost(panel, cfg)

    print("Training LSTM per ticker...")
    lstm_results = train_lstm_per_ticker(close, cfg)
    print(f"LSTM trained on {len(lstm_results)} tickers")

    today = build_today_panel(panel, xgb_model, lstm_results, close, volume, cfg)

    if regime_combined == "BEAR":
        print("\nregime=BEAR, skipping all streams")
        momentum_picks = today.iloc[0:0]
        ml_picks = today.iloc[0:0]
        ensemble_picks = today.iloc[0:0]
    else:
        momentum_picks = pick_momentum_stream(today, cfg)
        ml_picks = pick_ml_stream(today, cfg)
        ensemble_picks = pick_ensemble_stream(today, cfg)

    print_stream("MOMENTUM", momentum_picks)
    print_stream("ML", ml_picks)
    print_stream("ENSEMBLE", ensemble_picks)

    momentum_intents = build_trade_intents(momentum_picks, "momentum", cfg)
    ml_intents = build_trade_intents(ml_picks, "ml", cfg)
    ensemble_intents = build_trade_intents(ensemble_picks, "ensemble", cfg)

    TESTS_DIR.mkdir(parents=True, exist_ok=True)
    (TESTS_DIR / "momentum_intents_momentum.json").write_text(
        json.dumps(momentum_intents, indent=2)
    )
    (TESTS_DIR / "momentum_intents_ml.json").write_text(
        json.dumps(ml_intents, indent=2)
    )
    (TESTS_DIR / "momentum_intents_ensemble.json").write_text(
        json.dumps(ensemble_intents, indent=2)
    )

    combined_rows = []
    for label, df in [
        ("momentum", momentum_picks),
        ("ml", ml_picks),
        ("ensemble", ensemble_picks),
    ]:
        if df.empty:
            continue
        tmp = df.copy()
        tmp["source"] = label
        combined_rows.append(tmp)
    if combined_rows:
        combined = pd.concat(combined_rows)
        combined.to_csv(TESTS_DIR / "momentum_picks.csv", index=False)
    else:
        (TESTS_DIR / "momentum_picks.csv").write_text("")

    append_pick_ledger(momentum_picks, "momentum", regime_ma, regime_hmm, regime_combined)
    append_pick_ledger(ml_picks, "ml", regime_ma, regime_hmm, regime_combined)
    append_pick_ledger(ensemble_picks, "ensemble", regime_ma, regime_hmm, regime_combined)
    print(f"\nLedgers appended to {LEDGER_DIR}")

    if execute is None:
        return

    if regime_combined == "NEUTRAL" and not force_neutral:
        print("\n[execute] regime=NEUTRAL, pass --force-neutral to proceed")
        return
    if regime_combined == "BEAR":
        print("\n[execute] regime=BEAR, refusing to execute")
        return

    to_fire: List[Dict[str, Any]] = []
    if execute in ("momentum", "all"):
        to_fire.extend(momentum_intents)
    if execute in ("ml", "all"):
        to_fire.extend(ml_intents)
    if execute in ("ensemble", "all"):
        to_fire.extend(ensemble_intents)

    print(f"\n[execute] stream={execute} firing {len(to_fire)} intents")
    execute_intents(to_fire)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--min-conf", type=float, default=0.50)
    p.add_argument("--min-momentum", type=int, default=60)
    p.add_argument(
        "--execute",
        choices=["momentum", "ml", "ensemble", "all"],
        default=None,
        help="fire execute_trade for the named stream (off by default)",
    )
    p.add_argument(
        "--force-neutral",
        action="store_true",
        help="allow --execute when combined regime is NEUTRAL",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    cfg = Config(
        top_n=args.top_n,
        min_confidence=args.min_conf,
        min_momentum_score=args.min_momentum,
    )
    run(cfg, execute=args.execute, force_neutral=args.force_neutral)
