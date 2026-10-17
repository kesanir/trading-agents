from __future__ import annotations

import math
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

MAG7 = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]


def _safe_float(v) -> Optional[float]:
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


def _pct_change(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base == 0:
        return None
    return ((current - base) / base) * 100.0


def _fmt_num(v: Optional[float], digits: int = 2) -> str:
    return "N/A" if v is None else f"{v:.{digits}f}"


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    return "N/A" if v is None else f"{v:.{digits}f}%"


def _classify_overnight_position(
    overnight_high: Optional[float],
    overnight_low: Optional[float],
    prior_day_high: Optional[float],
    prior_day_low: Optional[float],
) -> str:
    if None in (overnight_high, overnight_low, prior_day_high, prior_day_low):
        return "UNKNOWN"

    if overnight_high > prior_day_high:
        return "ABOVE RANGE"
    if overnight_low < prior_day_low:
        return "BELOW RANGE"
    return "INSIDE RANGE"


def _calc_expected_move(spot: Optional[float], iv: Optional[float], dte: int = 1) -> Dict[str, Optional[float]]:
    if spot is None or iv is None or spot <= 0 or iv <= 0:
        return {
            "spot": spot,
            "iv": iv,
            "dte": dte,
            "expected_move_abs": None,
            "expected_move_pct": None,
        }

    em_abs = spot * iv * math.sqrt(dte / 365.0)
    em_pct = (em_abs / spot) * 100.0
    return {
        "spot": spot,
        "iv": iv,
        "dte": dte,
        "expected_move_abs": em_abs,
        "expected_move_pct": em_pct,
    }


def _nearest_future_expiry(expiries: List[str]) -> Optional[str]:
    today = datetime.now().date()
    valid = []
    for e in expiries:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
            if d >= today:
                valid.append(d)
        except Exception:
            continue
    if not valid:
        return None
    return min(valid).strftime("%Y-%m-%d")


def _get_expected_move_from_yfinance(ticker: str) -> Dict[str, Any]:
    """
    Development fallback.
    Replace later with Polygon/Alpaca IV source without changing caller interface.
    """
    result = {
        "ticker": ticker,
        "spot": None,
        "iv": None,
        "dte": 1,
        "expected_move_abs": None,
        "expected_move_pct": None,
        "source": "UNAVAILABLE",
    }

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="2d", interval="1d", auto_adjust=False)
        if hist.empty:
            return result

        spot = _safe_float(hist["Close"].iloc[-1])
        result["spot"] = spot

        expiries = list(tk.options or [])
        expiry = _nearest_future_expiry(expiries)
        if not expiry:
            return result

        dte = max((datetime.strptime(expiry, "%Y-%m-%d").date() - datetime.now().date()).days, 1)
        chain = tk.option_chain(expiry)

        frames = []
        if not chain.calls.empty:
            calls = chain.calls.copy()
            calls["side"] = "C"
            frames.append(calls)
        if not chain.puts.empty:
            puts = chain.puts.copy()
            puts["side"] = "P"
            frames.append(puts)

        if not frames:
            return result

        opt = pd.concat(frames, ignore_index=True)
        if "strike" not in opt.columns or "impliedVolatility" not in opt.columns:
            return result

        opt["dist"] = (opt["strike"] - spot).abs()
        opt = opt.sort_values("dist")
        atm = opt.head(4)
        iv = _safe_float(atm["impliedVolatility"].median())

        em = _calc_expected_move(spot, iv, dte)
        result.update(em)
        result["source"] = "YFINANCE_OPTION_CHAIN"
        return result

    except Exception as e:
        logger.warning("Expected move failed for %s: %s", ticker, e)
        return result


def _download_intraday(ticker: str, period: str = "5d", interval: str = "5m") -> pd.DataFrame:
    try:
        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            prepost=True,
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        if "Datetime" in df.columns:
            df["Datetime"] = pd.to_datetime(df["Datetime"], utc=True, errors="coerce")
        elif "Date" in df.columns:
            df["Datetime"] = pd.to_datetime(df["Date"], utc=True, errors="coerce")
        return df
    except Exception as e:
        logger.warning("Intraday download failed for %s: %s", ticker, e)
        return pd.DataFrame()


def _download_daily(ticker: str, period: str = "10d") -> pd.DataFrame:
    try:
        df = yf.download(
            ticker,
            period=period,
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.reset_index()
        return df
    except Exception as e:
        logger.warning("Daily download failed for %s: %s", ticker, e)
        return pd.DataFrame()


def _build_price_metrics(ticker: str) -> Dict[str, Any]:
    out = {
        "ticker": ticker,
        "prev_close": None,
        "last_price": None,
        "premarket_price": None,
        "gap_pct": None,
        "prior_day_high": None,
        "prior_day_low": None,
        "overnight_high": None,
        "overnight_low": None,
        "overnight_position": "UNKNOWN",
    }

    daily = _download_daily(ticker)
    intraday = _download_intraday(ticker)

    if daily.empty or len(daily) < 2:
        return out

    out["prev_close"] = _safe_float(daily.iloc[-2]["Close"])
    out["prior_day_high"] = _safe_float(daily.iloc[-2]["High"])
    out["prior_day_low"] = _safe_float(daily.iloc[-2]["Low"])
    out["last_price"] = _safe_float(daily.iloc[-1]["Close"])

    if intraday.empty or "Datetime" not in intraday.columns:
        out["gap_pct"] = _pct_change(out["last_price"], out["prev_close"])
        return out

    intraday = intraday.dropna(subset=["Datetime"]).sort_values("Datetime")
    latest_ts = intraday.iloc[-1]["Datetime"]
    current_date = latest_ts.date()

    # Approx regular-session open in UTC; acceptable for this stage
    session_start = pd.Timestamp(f"{current_date} 13:30:00", tz="UTC")
    today_rows = intraday[intraday["Datetime"].dt.date == current_date]
    premarket = today_rows[today_rows["Datetime"] < session_start]

    if not premarket.empty:
        out["premarket_price"] = _safe_float(premarket.iloc[-1]["Close"])
        out["overnight_high"] = _safe_float(premarket["High"].max())
        out["overnight_low"] = _safe_float(premarket["Low"].min())
    else:
        out["premarket_price"] = out["last_price"]

    ref_price = out["premarket_price"] or out["last_price"]
    out["gap_pct"] = _pct_change(ref_price, out["prev_close"])
    out["overnight_position"] = _classify_overnight_position(
        out["overnight_high"],
        out["overnight_low"],
        out["prior_day_high"],
        out["prior_day_low"],
    )
    return out


def _get_vix_level() -> Optional[float]:
    try:
        df = yf.download("^VIX", period="2d", interval="1d", auto_adjust=False, progress=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if df.empty:
            return None
        return _safe_float(df["Close"].iloc[-1])
    except Exception:
        return None


def _classify_vix_regime(vix: Optional[float]) -> str:
    if vix is None:
        return "UNKNOWN"
    if vix < 15:
        return "LOW VOL"
    if vix < 22:
        return "NORMAL VOL"
    if vix < 30:
        return "HIGH VOL"
    return "EXTREME VOL"


def _compute_no_trade_zone(open_ref: Optional[float], em_abs: Optional[float]) -> Dict[str, Optional[float]]:
    if open_ref is None or em_abs is None:
        return {"low": None, "high": None}
    band = 0.25 * em_abs
    return {"low": round(open_ref - band, 2), "high": round(open_ref + band, 2)}


def _compute_trend_day_probability(
    qqq_gap_pct: Optional[float],
    overnight_position: str,
    vix_level: Optional[float],
    qqq_expected_move_pct: Optional[float],
) -> Optional[float]:
    score = 0
    max_score = 5

    if qqq_gap_pct is not None and abs(qqq_gap_pct) >= 0.70:
        score += 1
    if overnight_position in ("ABOVE RANGE", "BELOW RANGE"):
        score += 1
    if vix_level is not None and vix_level < 22:
        score += 1
    if qqq_expected_move_pct is not None and qqq_expected_move_pct >= 1.2:
        score += 1
    if qqq_gap_pct is not None and overnight_position == "ABOVE RANGE" and qqq_gap_pct > 0:
        score += 1
    elif qqq_gap_pct is not None and overnight_position == "BELOW RANGE" and qqq_gap_pct < 0:
        score += 1

    return round(score / max_score, 2)


def _compute_pin_range_probability(
    qqq_gap_pct: Optional[float],
    overnight_position: str,
    qqq_expected_move_pct: Optional[float],
) -> str:
    score = 0
    if qqq_gap_pct is not None and abs(qqq_gap_pct) < 0.40:
        score += 1
    if overnight_position == "INSIDE RANGE":
        score += 1
    if qqq_expected_move_pct is not None and qqq_expected_move_pct < 1.1:
        score += 1

    if score >= 3:
        return "HIGH"
    if score == 2:
        return "MED"
    return "LOW"


def _score_setup(price_row: Dict[str, Any], em_row: Dict[str, Any]) -> Dict[str, Any]:
    gap_pct = price_row.get("gap_pct") or 0.0
    em_pct = em_row.get("expected_move_pct") or 0.0
    overnight = price_row.get("overnight_position", "UNKNOWN")

    score = 0.0
    score += min(abs(gap_pct), 2.0) * 0.8
    score += min(em_pct, 5.0) * 0.5

    if overnight in ("ABOVE RANGE", "BELOW RANGE"):
        score += 1.5
    elif overnight == "INSIDE RANGE":
        score += 0.5

    bias = "NEUT"
    if gap_pct > 0.15:
        bias = "BULL"
    elif gap_pct < -0.15:
        bias = "BEAR"

    if overnight == "ABOVE RANGE" and gap_pct > 0.4:
        setup = "OPEN DRIVE LONG"
        bias = "BULL"
        score += 1.0
    elif overnight == "BELOW RANGE" and gap_pct < -0.4:
        setup = "BREAKDOWN SHORT"
        bias = "BEAR"
        score += 1.0
    elif overnight == "INSIDE RANGE" and abs(gap_pct) < 0.4:
        setup = "RANGE / PIN"
        bias = "NEUT"
    elif gap_pct > 0:
        setup = "BULLISH EXPANSION WATCH"
    elif gap_pct < 0:
        setup = "BEARISH EXPANSION WATCH"
    else:
        setup = "NEUTRAL WATCH"

    if score >= 4.0:
        confidence = "HIGH"
    elif score >= 2.5:
        confidence = "MED"
    else:
        confidence = "LOW"

    return {
        "ticker": price_row["ticker"],
        "setup": setup,
        "bias": bias,
        "confidence": confidence,
        "score": round(score, 2),
    }


def build_enhanced_premarket_metrics(base_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Return only additive data. Caller merges into existing report object.
    """
    tickers = ["QQQ"] + MAG7

    price_data: Dict[str, Dict[str, Any]] = {}
    expected_moves: Dict[str, Dict[str, Any]] = {}

    for ticker in tickers:
        price_data[ticker] = _build_price_metrics(ticker)

    for ticker in tickers:
        expected_moves[ticker] = _get_expected_move_from_yfinance(ticker)

    vix = _get_vix_level()
    qqq_price = price_data["QQQ"]
    qqq_em = expected_moves["QQQ"]

    open_ref = qqq_price.get("premarket_price") or qqq_price.get("last_price")
    no_trade = _compute_no_trade_zone(open_ref, qqq_em.get("expected_move_abs"))
    trend_prob = _compute_trend_day_probability(
        qqq_gap_pct=qqq_price.get("gap_pct"),
        overnight_position=qqq_price.get("overnight_position", "UNKNOWN"),
        vix_level=vix,
        qqq_expected_move_pct=qqq_em.get("expected_move_pct"),
    )
    pin_prob = _compute_pin_range_probability(
        qqq_gap_pct=qqq_price.get("gap_pct"),
        overnight_position=qqq_price.get("overnight_position", "UNKNOWN"),
        qqq_expected_move_pct=qqq_em.get("expected_move_pct"),
    )

    setups = [_score_setup(price_data[t], expected_moves[t]) for t in MAG7]
    setups = sorted(setups, key=lambda x: x["score"], reverse=True)

    return {
        "enhanced_premarket": {
            "vix": vix,
            "volatility_regime": _classify_vix_regime(vix),
            "qqq_structure": {
                **qqq_price,
                "expected_move_abs": qqq_em.get("expected_move_abs"),
                "expected_move_pct": qqq_em.get("expected_move_pct"),
                "expected_move_source": qqq_em.get("source"),
                "no_trade_zone_low": no_trade["low"],
                "no_trade_zone_high": no_trade["high"],
                "trend_day_probability": trend_prob,
                "pin_range_probability": pin_prob,
            },
            "mag7_dashboard": [
                {
                    "ticker": t,
                    "gap_pct": price_data[t].get("gap_pct"),
                    "overnight_position": price_data[t].get("overnight_position"),
                    "expected_move_abs": expected_moves[t].get("expected_move_abs"),
                    "expected_move_pct": expected_moves[t].get("expected_move_pct"),
                    "expected_move_source": expected_moves[t].get("source"),
                }
                for t in MAG7
            ],
            "top3_setups": setups[:3],
            "all_setups": setups,
            "generated_at": datetime.utcnow().isoformat(),
        }
    }


def format_enhanced_premarket_section(report: Dict[str, Any]) -> str:
    enhanced = report.get("enhanced_premarket")
    if not enhanced:
        return ""

    qqq = enhanced["qqq_structure"]
    lines: List[str] = []
    lines.append("")
    lines.append("ENHANCED PREMARKET")
    lines.append("-" * 60)
    lines.append(f"VIX: { _fmt_num(enhanced.get('vix')) } | Regime: { enhanced.get('volatility_regime', 'UNKNOWN') }")
    lines.append("")
    lines.append("QQQ STRUCTURE")
    lines.append(f"Gap:              {_fmt_pct(qqq.get('gap_pct'))}")
    lines.append(f"Overnight Pos:    {qqq.get('overnight_position', 'UNKNOWN')}")
    lines.append(f"Expected Move:    ±{_fmt_num(qqq.get('expected_move_abs'))} ({_fmt_pct(qqq.get('expected_move_pct'))})")
    lines.append(f"No-Trade Zone:    {_fmt_num(qqq.get('no_trade_zone_low'))} - {_fmt_num(qqq.get('no_trade_zone_high'))}")
    trend_prob = qqq.get("trend_day_probability")
    trend_prob_pct = None if trend_prob is None else trend_prob * 100
    lines.append(f"Trend Day Prob:   {_fmt_pct(trend_prob_pct)}")
    lines.append(f"Pin/Range Prob:   {qqq.get('pin_range_probability', 'UNKNOWN')}")
    lines.append("")
    lines.append("MAG7 DASHBOARD")
    lines.append(f"{'Ticker':<8}{'Gap %':<12}{'Exp Move':<16}{'Exp %':<10}{'ON Pos'}")

    for row in enhanced.get("mag7_dashboard", []):
        lines.append(
            f"{row['ticker']:<8}"
            f"{_fmt_pct(row.get('gap_pct')):<12}"
            f"±{_fmt_num(row.get('expected_move_abs')):<15}"
            f"{_fmt_pct(row.get('expected_move_pct')):<10}"
            f"{row.get('overnight_position', 'UNKNOWN')}"
        )

    lines.append("")
    lines.append("TOP 3 SETUPS")
    lines.append(f"{'Ticker':<8}{'Setup':<26}{'Bias':<8}{'Conf':<8}{'Score'}")

    for row in enhanced.get("top3_setups", []):
        lines.append(
            f"{row['ticker']:<8}"
            f"{row['setup']:<26}"
            f"{row['bias']:<8}"
            f"{row['confidence']:<8}"
            f"{row['score']}"
        )

    return "\n".join(lines)


def format_enhanced_premarket_telegram(report: Dict[str, Any]) -> str:
    enhanced = report.get("enhanced_premarket")
    if not enhanced:
        return ""

    qqq = enhanced["qqq_structure"]
    trend_prob = qqq.get("trend_day_probability")
    trend_prob_pct = None if trend_prob is None else trend_prob * 100

    lines: List[str] = []
    lines.append("📈 *Enhanced Premarket*")
    lines.append(f"VIX: *{_fmt_num(enhanced.get('vix'))}* | {enhanced.get('volatility_regime', 'UNKNOWN')}")
    lines.append("")
    lines.append("*QQQ*")
    lines.append(f"Gap: *{_fmt_pct(qqq.get('gap_pct'))}*")
    lines.append(f"ON Position: *{qqq.get('overnight_position', 'UNKNOWN')}*")
    lines.append(f"Exp Move: *±{_fmt_num(qqq.get('expected_move_abs'))}* ({_fmt_pct(qqq.get('expected_move_pct'))})")
    lines.append(f"No-Trade: *{_fmt_num(qqq.get('no_trade_zone_low'))} - {_fmt_num(qqq.get('no_trade_zone_high'))}*")
    lines.append(f"Trend Day: *{_fmt_pct(trend_prob_pct)}*")
    lines.append(f"Pin/Range: *{qqq.get('pin_range_probability', 'UNKNOWN')}*")
    lines.append("")
    lines.append("*Top 3 Setups*")

    for row in enhanced.get("top3_setups", []):
        lines.append(
            f"- *{row['ticker']}* | {row['setup']} | {row['bias']} | {row['confidence']} | score={row['score']}"
        )

    return "\n".join(lines)