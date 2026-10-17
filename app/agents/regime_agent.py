# app/agents/regime_agent.py

from __future__ import annotations

import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from app.services.vix_apmem_service import (
    build_vix_apmem_signal,
    long_regime_boost,
    short_regime_boost,
    position_size_multiplier,
)

load_dotenv()

log = logging.getLogger("app.agents.regime_agent")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

MIN_STRENGTH = float(os.getenv("SCANNER_MIN_STRENGTH", "0.15"))
MIN_CONTRACTS = int(os.getenv("SCANNER_MIN_CONTRACTS", "50"))
MAX_DTE = int(os.getenv("SCANNER_MAX_DTE", "45"))
MAX_DTE_FALLBACK = int(os.getenv("SCANNER_MAX_DTE_FALLBACK", "90"))
MAX_WORKERS = int(os.getenv("SCANNER_MAX_WORKERS", "6"))

# -----------------------------------------------------------------------------
# Optional universes
# -----------------------------------------------------------------------------

MAG7: List[str] = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA"]

NDX_MINI: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
    "AMD", "NFLX", "QCOM", "ADBE", "COST", "AMGN", "INTU"
]

UNIVERSES: Dict[str, List[str]] = {
    "mag7": MAG7,
    "ndx_mini": NDX_MINI,
}

# -----------------------------------------------------------------------------
# Dataclass
# -----------------------------------------------------------------------------

@dataclass
class TickerRegime:
    ticker: str
    spot: float
    contracts: int
    regime: str
    strength: float
    net_gex_b: float
    zero_gamma: Optional[float]
    call_wall: Optional[float]
    put_wall: Optional[float]
    dist_to_flip: Optional[float]
    slope_regime: str
    momentum: str
    gex_skew: float
    score: float

    # Macro overlay fields
    score_base: float = 0.0
    score_macro: float = 0.0
    regime_score: int = 50
    trade_bias: str = "WAIT"
    size_mult: float = 1.0

    levels: dict = field(default_factory=dict)
    slope: dict = field(default_factory=dict)
    iv_analytics: dict = field(default_factory=dict)
    unusual_vol: list = field(default_factory=list)
    error: str = ""

# -----------------------------------------------------------------------------
# Core bootstrap
# -----------------------------------------------------------------------------

def _bootstrap_one(
    ticker: str,
    min_dte: int = 0,
    max_dte: int = MAX_DTE,
    vol_signal: Optional[Any] = None,
) -> TickerRegime:
    """
    Bootstrap one ticker using the existing GEX engine and return a clean regime
    summary, enhanced with a shared macro VIX+APMEM overlay.
    """
    from app.features.gex_core import (
        GEXEngine,
        fast_bootstrap,
        compute_key_levels,
        compute_gamma_slope,
    )

    engine = GEXEngine(underlying=ticker)

    try:
        fast_bootstrap(engine, ticker, min_dte=min_dte, max_dte=max_dte)
    except Exception as e:
        return TickerRegime(
            ticker=ticker,
            spot=0.0,
            contracts=0,
            regime="",
            strength=0.0,
            net_gex_b=0.0,
            zero_gamma=None,
            call_wall=None,
            put_wall=None,
            dist_to_flip=None,
            slope_regime="",
            momentum="",
            gex_skew=0.0,
            score=0.0,
            score_base=0.0,
            score_macro=0.0,
            regime_score=50,
            trade_bias="WAIT",
            size_mult=1.0,
            error=str(e),
        )

    # Retry with wider DTE if sparse
    if len(engine.contract_data) == 0 and max_dte < MAX_DTE_FALLBACK:
        log.info(
            "[%s] 0 contracts at max_dte=%d, retrying max_dte=%d",
            ticker, max_dte, MAX_DTE_FALLBACK
        )
        engine2 = GEXEngine(underlying=ticker)
        try:
            fast_bootstrap(engine2, ticker, min_dte=min_dte, max_dte=MAX_DTE_FALLBACK)
            if len(engine2.contract_data) > 0:
                engine = engine2
        except Exception:
            pass

    if engine.spot_price <= 0 or len(engine.contract_data) < MIN_CONTRACTS:
        return TickerRegime(
            ticker=ticker,
            spot=round(float(engine.spot_price or 0), 2),
            contracts=len(engine.contract_data),
            regime="",
            strength=0.0,
            net_gex_b=0.0,
            zero_gamma=None,
            call_wall=None,
            put_wall=None,
            dist_to_flip=None,
            slope_regime="",
            momentum="",
            gex_skew=0.0,
            score=0.0,
            score_base=0.0,
            score_macro=0.0,
            regime_score=50,
            trade_bias="WAIT",
            size_mult=1.0,
            error=f"insufficient data: spot={engine.spot_price:.2f} contracts={len(engine.contract_data)}",
        )

    gex_map = engine.compute_gex_by_strike()
    levels = compute_key_levels(gex_map, engine.spot_price)
    slope = compute_gamma_slope(gex_map, engine.spot_price)
    iv_data = engine.compute_iv_rank()
    unusual = engine.compute_unusual_volume(top_n=10)

    if not levels:
        return TickerRegime(
            ticker=ticker,
            spot=round(engine.spot_price, 2),
            contracts=len(engine.contract_data),
            regime="",
            strength=0.0,
            net_gex_b=0.0,
            zero_gamma=None,
            call_wall=None,
            put_wall=None,
            dist_to_flip=None,
            slope_regime="",
            momentum="",
            gex_skew=0.0,
            score=0.0,
            score_base=0.0,
            score_macro=0.0,
            regime_score=50,
            trade_bias="WAIT",
            size_mult=1.0,
            error="compute_key_levels returned empty",
        )

    strength = abs(levels.get("regime_strength", 0.0))
    dist = levels.get("dist_to_flip_pct")
    slope_regime = (slope.get("at_spot") or {}).get("regime", "flat")
    momentum = (slope.get("near_spot") or {}).get("slope_momentum", "neutral")
    skew = levels.get("gex_skew", 1.0)

    # ------------------------------------------------------------------
    # Base micro score
    # ------------------------------------------------------------------
    base_score = strength * 0.40

    if dist is not None:
        prox = max(0.0, 1.0 - abs(dist) / 5.0)
        base_score += prox * 0.30

    if momentum in ("accelerating", "decelerating"):
        base_score += 0.20

    base_score += min(abs(math.log(skew if skew > 0 else 1e-9)), 1.0) * 0.10

    # ------------------------------------------------------------------
    # Macro overlay (shared global VIX/APMEM signal)
    # ------------------------------------------------------------------
    regime_score = 50
    trade_bias = "WAIT"
    size_mult = 1.0
    macro_adj = 0.0

    if vol_signal is not None:
        try:
            regime_score = int(vol_signal.score)
            trade_bias = str(vol_signal.bias)
            size_mult = float(position_size_multiplier(regime_score))

            # POS gamma regime tends to support longs more
            # anything else gets treated as short-side / defensive
            direction = "LONG" if levels.get("regime", "").upper() == "POSITIVE" else "SHORT"

            if direction == "LONG":
                macro_adj = long_regime_boost(regime_score)
            else:
                macro_adj = short_regime_boost(regime_score)

            # Keep macro effect modest so it nudges instead of dominating
            macro_adj *= 0.50

        except Exception as e:
            log.warning("[%s] macro overlay failed: %s", ticker, e)

    score = base_score + macro_adj

    return TickerRegime(
        ticker=ticker,
        spot=round(engine.spot_price, 2),
        contracts=len(engine.contract_data),
        regime=levels.get("regime", ""),
        strength=round(strength, 4),
        net_gex_b=round(levels.get("net_gex_b", 0.0), 4),
        zero_gamma=levels.get("zero_gamma"),
        call_wall=levels.get("call_wall"),
        put_wall=levels.get("put_wall"),
        dist_to_flip=dist,
        slope_regime=slope_regime,
        momentum=momentum,
        gex_skew=skew,
        score=round(score, 4),
        score_base=round(base_score, 4),
        score_macro=round(macro_adj, 4),
        regime_score=regime_score,
        trade_bias=trade_bias,
        size_mult=round(size_mult, 2),
        levels=levels,
        slope=slope,
        iv_analytics=iv_data,
        unusual_vol=unusual,
    )

# -----------------------------------------------------------------------------
# Filters
# -----------------------------------------------------------------------------

def filter_candidates(results: List[TickerRegime]) -> List[TickerRegime]:
    """
    Keep only actionable regime candidates.
    """
    candidates: List[TickerRegime] = []

    for r in results:
        if r.error:
            continue
        if r.spot <= 0:
            continue
        if r.contracts < MIN_CONTRACTS:
            continue
        if r.strength < MIN_STRENGTH:
            continue
        candidates.append(r)

    candidates.sort(key=lambda x: x.score, reverse=True)
    return candidates

# -----------------------------------------------------------------------------
# Public agent functions
# -----------------------------------------------------------------------------

def classify_regime(
    symbol: str,
    min_dte: int = 0,
    max_dte: int = MAX_DTE,
) -> Dict[str, Any]:
    """
    Single-symbol regime classifier.
    """
    vol_signal = None
    try:
        vol_signal = build_vix_apmem_signal()
    except Exception as e:
        log.warning("Failed to build global VIX/APMEM signal for classify_regime: %s", e)

    result = _bootstrap_one(symbol, min_dte=min_dte, max_dte=max_dte, vol_signal=vol_signal)
    return asdict(result)


def scan_universe(
    tickers: List[str],
    min_dte: int = 0,
    max_dte: int = MAX_DTE,
    max_workers: int = MAX_WORKERS,
    top_n: int = 10,
) -> Dict[str, Any]:
    """
    Multi-symbol regime scan with one shared global VIX/APMEM snapshot.
    """
    results: List[TickerRegime] = []

    # Build macro signal once for the whole scan
    vol_signal = None
    try:
        vol_signal = build_vix_apmem_signal()
        log.info(
            "Global regime | VIX=%.2f | APMEM=%.4f | Score=%d | Regime=%s | Bias=%s",
            vol_signal.vix,
            vol_signal.apmem_vol,
            vol_signal.score,
            vol_signal.regime,
            vol_signal.bias,
        )
    except Exception as e:
        log.warning("Failed to build global VIX/APMEM signal: %s", e)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_bootstrap_one, ticker, min_dte, max_dte, vol_signal): ticker
            for ticker in tickers
        }

        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as e:
                results.append(
                    TickerRegime(
                        ticker=ticker,
                        spot=0.0,
                        contracts=0,
                        regime="",
                        strength=0.0,
                        net_gex_b=0.0,
                        zero_gamma=None,
                        call_wall=None,
                        put_wall=None,
                        dist_to_flip=None,
                        slope_regime="",
                        momentum="",
                        gex_skew=0.0,
                        score=0.0,
                        score_base=0.0,
                        score_macro=0.0,
                        regime_score=50,
                        trade_bias="WAIT",
                        size_mult=1.0,
                        error=str(e),
                    )
                )

    candidates = filter_candidates(results)

    return {
        "total": len(tickers),
        "loaded": sum(1 for r in results if not r.error),
        "candidates": [asdict(c) for c in candidates[:top_n]],
        "errors": [asdict(r) for r in results if r.error],
        "global_regime": vol_signal.to_dict() if vol_signal is not None else None,
    }


def scan_named_universe(
    universe_name: str = "mag7",
    min_dte: int = 0,
    max_dte: int = MAX_DTE,
    top_n: int = 10,
) -> Dict[str, Any]:
    """
    Convenience wrapper for predefined universes.
    """
    tickers = UNIVERSES.get(universe_name.lower())
    if not tickers:
        raise ValueError(f"Unknown universe '{universe_name}'. Available: {list(UNIVERSES)}")

    return scan_universe(
        tickers=tickers,
        min_dte=min_dte,
        max_dte=max_dte,
        top_n=top_n,
    )