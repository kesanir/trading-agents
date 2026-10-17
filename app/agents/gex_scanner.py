"""
gex_scanner.py — GEX-driven options screener for NDX100 and SP100
===================================================================
Scans a universe of tickers using GEX analysis, generates AI trade ideas
via Claude, and optionally auto-executes through Alpaca.

Usage (standalone):
    python gex_scanner.py --universe ndx100 --execute
    python gex_scanner.py --universe sp100 --top 10 --no-execute --output scan.json

Usage (integrated with gex_server.py):
    POST /api/scan          { "universe": "ndx100", "execute": false }
    GET  /api/scan/status
    GET  /api/scan/results

Environment (.env):
    POLYGON_API_KEY          required — options chain data
    ALPACA_API_KEY           required for execution
    ALPACA_SECRET_KEY        required for execution
    SCANNER_MAX_WORKERS      parallel bootstrap threads   (default 6)
    SCANNER_TOP_N            ideas to generate per run    (default 15)
    SCANNER_MIN_STRENGTH     min |regime_strength| filter (default 0.15)
    SCANNER_MIN_CONTRACTS    min contracts loaded         (default 50)
    SCANNER_MAX_DTE          options DTE for bootstrap    (default 45)
    SCANNER_MAX_DTE_FALLBACK wider DTE retry if 0 ctrs   (default 90)
    SCANNER_EXECUTE          "true" to auto-execute       (default false)
    SCANNER_MIN_CONFIDENCE   min confidence to execute    (default "HIGH")
    SCANNER_MAX_TRADES       max trades per scan run      (default 3)
    GEX_DEFAULT_IV_SPX       runtime IV override for SPX  (e.g. 0.185)
"""

import asyncio
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("gex.scanner")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

# ---------------------------------------------------------------------------
# Universe definitions  — as of Q1 2026; update quarterly
# ---------------------------------------------------------------------------

NDX100: List[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO",
    # Consumer / retail
    "COST", "NFLX", "SBUX", "ORLY", "ROST", "DLTR", "LULU", "MAR", "BKNG",
    # Semiconductors
    "AMD", "QCOM", "INTC", "TXN", "AMAT", "MU", "LRCX", "KLAC", "ADI",
    "SNPS", "CDNS", "MCHP", "NXPI", "MRVL", "ON", "ASML",
    # Software / cloud
    "ADBE", "INTU", "CRM", "ORCL", "ANSS", "ADSK", "WDAY", "NOW", "TEAM",
    "PANW", "CRWD", "FTNT", "ZS", "DDOG", "ZM", "ADP", "PAYX", "CTSH",
    # Healthcare / biotech
    "AMGN", "GILD", "VRTX", "REGN", "BIIB", "MRNA", "IDXX", "ILMN",
    "DXCM", "ISRG", "AZN", "GEHC",
    # Internet / media
    "PYPL", "EBAY", "MELI", "TTD", "TTWO", "EA", "WBD", "MTCH",
    # Industrials / other
    "HON", "CSX", "PCAR", "FAST", "ODFL", "VRSK", "CPRT", "CSGP",
    "TMUS", "CMCSA", "CHTR", "NDAQ", "CDW", "CEG", "FANG",
    "EXC", "XEL", "KDP", "KHC", "MDLZ", "MNST", "PEP",
    "SIRI", "HSIC", "ALGN", "GFS",
]
# Deduplicate (ADP appeared twice above)
NDX100 = list(dict.fromkeys(NDX100))

SP100: List[str] = [
    # Mega-cap tech
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "GOOG",
    # Finance
    "JPM", "BAC", "GS", "MS", "WFC", "AXP", "BX", "SCHW", "USB", "MET", "V", "MA",
    # Healthcare
    "UNH", "JNJ", "LLY", "MRK", "ABBV", "ABT", "BMY", "PFE", "CI", "DHR",
    # Consumer
    "WMT", "HD", "TGT", "TJX", "LOW", "MCD", "NKE", "MO", "PM", "KO", "PG",
    "PEP", "COST", "SBUX",
    # Energy
    "XOM", "CVX", "COP", "SLB",
    # Industrials
    "CAT", "DE", "BA", "RTX", "GE", "ETN", "EMR", "MMM", "HON",
    "GD", "LMT", "UPS", "FDX", "UNP", "NSC", "CSX",
    # Tech
    "AVGO", "ADBE", "CRM", "ORCL", "IBM", "ACN", "CSCO", "INTC", "INTU",
    "QCOM", "TXN", "NFLX", "AMD", "PANW", "NOW",
    # Healthcare (more)
    "AMGN", "GILD", "VRTX", "REGN", "ISRG", "TMO",
    # Telecom / media
    "T", "VZ", "TMUS", "CMCSA", "DIS",
    # Utilities
    "NEE", "DUK", "SO",
    # Other
    "SPG", "MSCI", "F", "GM", "DOW", "ABT", "PYPL", "AXP",
    # BRK.B excluded — non-standard OCC symbol, no listed options
]
# Deduplicate
SP100 = list(dict.fromkeys(SP100))

UNIVERSES = {
    "ndx100": NDX100,
    "sp100":  SP100,
}

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_WORKERS      = int(os.getenv("SCANNER_MAX_WORKERS",    "6"))
TOP_N            = int(os.getenv("SCANNER_TOP_N",          "15"))
MIN_STRENGTH     = float(os.getenv("SCANNER_MIN_STRENGTH", "0.15"))
MIN_CONTRACTS    = int(os.getenv("SCANNER_MIN_CONTRACTS",  "50"))
MAX_DTE          = int(os.getenv("SCANNER_MAX_DTE",        "45"))
MAX_DTE_FALLBACK = int(os.getenv("SCANNER_MAX_DTE_FALLBACK","90"))  # retry if 0 contracts
AUTO_EXECUTE     = os.getenv("SCANNER_EXECUTE",            "false").lower() == "true"
MIN_CONFIDENCE   = os.getenv("SCANNER_MIN_CONFIDENCE",     "HIGH").upper()
MAX_TRADES       = int(os.getenv("SCANNER_MAX_TRADES",     "3"))

# Naked single-leg execution gate.
# When true, long puts/calls are allowed under strict conditions (negative gamma +
# wall breach + high confidence).  Short naked options are NEVER allowed.
ALLOW_NAKED      = os.getenv("SCANNER_ALLOW_NAKED",        "false").lower() == "true"


# ---------------------------------------------------------------------------
# Scan result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TickerGEX:
    """GEX snapshot for one ticker after bootstrap."""
    ticker:       str
    spot:         float
    contracts:    int
    regime:       str
    strength:     float          # |net / Σ|GEX||
    net_gex_b:    float
    zero_gamma:   Optional[float]
    call_wall:    Optional[float]
    put_wall:     Optional[float]
    dist_to_flip: Optional[float]
    slope_regime: str            # building / collapsing / flat
    momentum:     str            # accelerating / decelerating / neutral
    gex_skew:     float
    score:        float          # composite screening score
    levels:       dict  = field(default_factory=dict)
    slope:        dict  = field(default_factory=dict)
    iv_analytics: dict  = field(default_factory=dict)   # IV rank, ATM IV, skew
    unusual_vol:  list  = field(default_factory=list)   # top vol/OI anomalies
    error:        str   = ""


@dataclass
class ScanResult:
    """Full result of one scan run."""
    universe:     str
    started_at:   str
    finished_at:  str
    total_tickers: int
    loaded:       int
    screened:     int
    candidates:   List[TickerGEX]   = field(default_factory=list)
    trade_ideas:  List[dict]        = field(default_factory=list)
    executions:   List[dict]        = field(default_factory=list)
    errors:       List[str]         = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bootstrap one ticker
# ---------------------------------------------------------------------------

def _bootstrap_one(ticker: str, min_dte: int = 0, max_dte: int = MAX_DTE
                   ) -> TickerGEX:
    """
    Load one ticker's options chain into a fresh GEXEngine.
    Returns a TickerGEX summary or one with error set on failure.
    """
    from gex_core import GEXEngine, fast_bootstrap, compute_key_levels, compute_gamma_slope

    engine = GEXEngine(underlying=ticker)
    try:
        fast_bootstrap(engine, ticker, min_dte=min_dte, max_dte=max_dte)
    except Exception as e:
        return TickerGEX(
            ticker=ticker, spot=0, contracts=0, regime="", strength=0,
            net_gex_b=0, zero_gamma=None, call_wall=None, put_wall=None,
            dist_to_flip=None, slope_regime="", momentum="", gex_skew=0, score=0,
            error=str(e),
        )

    # ── DTE fallback: retry with wider window if 0 contracts returned ─────────
    # Happens for tickers where near-term expiries are sparse (e.g. KLAC, NXPI
    # outside earnings season) or when max_dte is very narrow.
    if len(engine.contract_data) == 0 and max_dte < MAX_DTE_FALLBACK:
        log.info("[%s] 0 contracts at max_dte=%d — retrying with max_dte=%d",
                 ticker, max_dte, MAX_DTE_FALLBACK)
        engine2 = GEXEngine(underlying=ticker)
        try:
            fast_bootstrap(engine2, ticker, min_dte=min_dte, max_dte=MAX_DTE_FALLBACK)
            if len(engine2.contract_data) > 0:
                engine = engine2
        except Exception:
            pass  # keep engine with 0 contracts — error reported below

    if engine.spot_price <= 0 or len(engine.contract_data) < MIN_CONTRACTS:
        return TickerGEX(
            ticker=ticker, spot=engine.spot_price,
            contracts=len(engine.contract_data), regime="", strength=0,
            net_gex_b=0, zero_gamma=None, call_wall=None, put_wall=None,
            dist_to_flip=None, slope_regime="", momentum="", gex_skew=0, score=0,
            error=f"insufficient data: spot={engine.spot_price:.2f} contracts={len(engine.contract_data)}",
        )

    gex_map = engine.compute_gex_by_strike()
    levels  = compute_key_levels(gex_map, engine.spot_price)
    slope   = compute_gamma_slope(gex_map, engine.spot_price)
    iv_data = engine.compute_iv_rank()
    unusual = engine.compute_unusual_volume(top_n=10)

    if not levels:
        return TickerGEX(
            ticker=ticker, spot=engine.spot_price,
            contracts=len(engine.contract_data), regime="", strength=0,
            net_gex_b=0, zero_gamma=None, call_wall=None, put_wall=None,
            dist_to_flip=None, slope_regime="", momentum="", gex_skew=0, score=0,
            error="compute_key_levels returned empty",
        )

    strength     = abs(levels.get("regime_strength", 0))
    dist         = levels.get("dist_to_flip_pct")
    slope_regime = (slope.get("at_spot") or {}).get("regime", "flat")
    momentum     = (slope.get("near_spot") or {}).get("slope_momentum", "neutral")

    # ── Composite screening score ────────────────────────────────────────────
    # Components:
    #   strength    — strong regime = high conviction GEX state
    #   proximity   — near zero-gamma = volatile zone, more edge from GEX plays
    #   momentum    — accelerating/decelerating = slope is meaningful
    #   skew        — imbalanced call/put GEX = directional bias available
    score = strength * 0.40
    if dist is not None:
        prox  = max(0, 1.0 - abs(dist) / 5.0)   # 5% band, peaks at dist=0
        score += prox * 0.30
    if momentum in ("accelerating", "decelerating"):
        score += 0.20
    skew = levels.get("gex_skew", 1.0)
    score += min(abs(_math_log(skew if skew > 0 else 1e-9)), 1.0) * 0.10

    return TickerGEX(
        ticker       = ticker,
        spot         = round(engine.spot_price, 2),
        contracts    = len(engine.contract_data),
        regime       = levels.get("regime", ""),
        strength     = round(strength, 4),
        net_gex_b    = round(levels.get("net_gex_b", 0), 4),
        zero_gamma   = levels.get("zero_gamma"),
        call_wall    = levels.get("call_wall"),
        put_wall     = levels.get("put_wall"),
        dist_to_flip = dist,
        slope_regime = slope_regime,
        momentum     = momentum,
        gex_skew     = levels.get("gex_skew", 1.0),
        score        = round(score, 4),
        levels       = levels,
        slope        = slope,
        iv_analytics = iv_data,
        unusual_vol  = unusual,
    )


def _math_log(x: float) -> float:
    import math
    return math.log(x)


# ---------------------------------------------------------------------------
# Screen: filter + rank candidates
# ---------------------------------------------------------------------------

def screen_candidates(results: List[TickerGEX]) -> List[TickerGEX]:
    """
    Filter to actionable GEX setups and rank by composite score.

    Hard filters (must pass all):
      - No error during bootstrap
      - contracts >= MIN_CONTRACTS
      - |regime_strength| >= MIN_STRENGTH
      - spot > 0

    Soft boosts (affect score but do NOT disqualify):
      - Slope regime building/collapsing → +score
      - Near zero-gamma (<2%) → +score
      - Momentum accelerating/decelerating → +score
      - Skew imbalance → +score

    Everything that passes hard filters is a candidate.
    The composite score determines which get trade ideas generated.
    """
    candidates = []
    rejected   = {"error": 0, "contracts": 0, "strength": 0, "spot": 0}

    for r in results:
        if r.error:
            rejected["error"] += 1
            continue
        if r.spot <= 0:
            rejected["spot"] += 1
            continue
        if r.contracts < MIN_CONTRACTS:
            rejected["contracts"] += 1
            continue
        if r.strength < MIN_STRENGTH:
            rejected["strength"] += 1
            continue
        candidates.append(r)

    candidates.sort(key=lambda x: x.score, reverse=True)
    log.info(
        "Screened %d results → %d candidates | rejected: error=%d spot=%d "
        "contracts<%d=%d strength<%.2f=%d",
        len(results), len(candidates),
        rejected["error"], rejected["spot"],
        MIN_CONTRACTS, rejected["contracts"],
        MIN_STRENGTH, rejected["strength"],
    )
    return candidates


# ---------------------------------------------------------------------------
# Build GEX data dict for generate_trade_ideas (mirrors _engine_to_dict)
# ---------------------------------------------------------------------------

def _candidate_to_gex_data(cand: TickerGEX) -> dict:
    """Package TickerGEX into the dict format expected by generate_trade_ideas."""
    return {
        "ticker":         cand.ticker,
        "spot":           cand.spot,
        "contracts":      cand.contracts,
        "levels":         cand.levels,
        "gamma_slope":    cand.slope,
        "iv_analytics":   cand.iv_analytics,    # IV rank, ATM IV, skew — now passed to prompt
        "unusual_volume": cand.unusual_vol,     # smart money flow — now passed to prompt
        "bar":            {},
        "by_expiry":      {},
        "gex_bucketed":   {},
        "gamma_flips":    [],
        "spot_source":    "scanner",
        "snapshots":      0,
    }


# ---------------------------------------------------------------------------
# Idea → Alpaca signal bridge
# ---------------------------------------------------------------------------

def idea_to_alpaca_signal(
    idea:             dict,
    ticker:           str,
    spot:             float,
    regime:           str,
    cand:             Optional["TickerGEX"] = None,
    bypass_confidence: bool = False,   # True for manual dashboard executes
) -> List[dict]:
    """
    Translate a Claude trade idea into a list of alpaca_options_trader signal dicts.

    Returns [] if the idea does not meet execution criteria.
    Returns [signal] for single-leg or 2-leg spread ideas.
    Returns [bull_put_signal, bear_call_signal] for Iron Condors (4 legs) —
      splits into two separate defined-risk credit spreads executed sequentially.
      Both legs have defined risk so there is no naked exposure.
    """
    direction  = (idea.get("direction")   or "").lower()
    trade_type = (idea.get("trade_type")  or "spread").lower()
    strategy   = (idea.get("strategy")    or "").lower()
    confidence = (idea.get("confidence")  or "").upper()
    legs       = idea.get("legs") or []
    tags       = [t.lower() for t in (idea.get("tags") or [])]

    # Confidence gate — applies to scanner auto-execute; bypassed for manual dashboard execute
    if not bypass_confidence:
        confidence_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        min_rank = confidence_rank.get(MIN_CONFIDENCE, 3)
        if confidence_rank.get(confidence, 0) < min_rank:
            log.info("[%s] Skipping %s — confidence %s below threshold %s",
                      ticker, strategy, confidence, MIN_CONFIDENCE)
            return []

    if not legs:
        log.info("[%s] Skipping %s — no legs", ticker, strategy)
        return []

    # IC / neutral strategies: direction may be "neutral" — treat as bull for execution
    # (positive regime + IC means we're range-bound, bull put spread is the lower-risk leg)
    if direction == "neutral":
        if len(legs) == 4:
            direction  = "bullish"   # remap for IC execution — pick put spread side
            alpaca_dir = "bull"
            log.info("[%s] Neutral IC → remapping direction to bullish for execution", ticker)
        else:
            log.info("[%s] Skipping %s — direction=neutral on non-IC (%d legs)",
                     ticker, strategy, len(legs))
            return []

    if direction not in ("bullish", "bearish"):
        log.info("[%s] Skipping %s — unrecognised direction '%s'", ticker, strategy, direction)
        return []

    alpaca_dir = "bull" if direction == "bullish" else "bear"

    # ── Single-leg path ───────────────────────────────────────────────────────
    if trade_type == "single_leg" or len(legs) == 1:
        if not ALLOW_NAKED:
            log.info("[%s] Skipping single-leg %s — SCANNER_ALLOW_NAKED=false", ticker, strategy)
            return []

        leg = legs[0]
        action      = (leg.get("action") or "").upper()
        option_type = (leg.get("type")   or "").upper()   # CALL | PUT
        expiry_dte  = int(leg.get("expiry_dte") or 14)
        strike      = leg.get("strike")

        # Only long options — never short naked
        if action != "BUY":
            log.info("[%s] Skipping single-leg — action=%s (only BUY allowed)", ticker, action)
            return []

        # Regime gate — negative GEX only (dealers amplify the move)
        if regime != "negative":
            log.info("[%s] Skipping single-leg — regime=%s (need negative)", ticker, regime)
            return []

        # Wall breach confirmation using candidate levels
        if cand:
            levels    = cand.levels or {}
            put_wall  = levels.get("put_wall")
            call_wall = levels.get("call_wall")

            if option_type == "PUT":
                # Long put valid when spot has broken below put wall
                if put_wall and spot > put_wall * 1.005:   # 0.5% tolerance
                    log.info("[%s] Skipping long put — spot %.2f above put_wall %.2f",
                              ticker, spot, put_wall)
                    return []
            elif option_type == "CALL":
                # Long call valid when spot has broken above call wall
                if call_wall and spot < call_wall * 0.995:
                    log.info("[%s] Skipping long call — spot %.2f below call_wall %.2f",
                              ticker, spot, call_wall)
                    return []

        # Slope confirmation — collapsing for puts, building for calls
        if cand and cand.slope:
            slope_regime = cand.slope_regime
            momentum     = cand.momentum
            if option_type == "PUT" and slope_regime not in ("collapsing", "flat"):
                log.debug("[%s] Skipping long put — slope_regime=%s", ticker, slope_regime)
                return []
            if option_type == "CALL" and slope_regime not in ("building", "flat"):
                log.debug("[%s] Skipping long call — slope_regime=%s", ticker, slope_regime)
                return []

        log.info("[%s] Single-leg approved: %s %s strike=%s dte=%d",
                 ticker, action, option_type, strike, expiry_dte)
        return [{
            "ticker":       ticker,
            "signal":       "BUY" if direction == "bullish" else "SELL",
            "xgb_prob":     0.75,
            "price":        spot,
            "regime":       regime,
            "strategy":     strategy,
            "direction":    alpaca_dir,
            "trade_type":   "single_leg",
            "option_type":  option_type.lower(),
            "side":         "buy",
            "strike":       float(strike) if strike else None,
            "expiry_dte":   expiry_dte,
            "legs":         legs,
            "auto":         True,
            "source":       "gex_scanner",
        }]

    # ── Spread path (2 legs) or Iron Condor (4 legs → single atomic order) ─────
    if len(legs) == 0:
        log.info("[%s] Skipping %s — no legs in idea", ticker, strategy)
        return []

    def _make_spread_signal(spread_legs: list, spread_dir: str) -> dict:
        """Build one 2-leg spread signal dict."""
        s_dir = "bull" if spread_dir == "bullish" else "bear"
        return {
            "ticker":     ticker,
            "signal":     "BUY" if spread_dir == "bullish" else "SELL",
            "xgb_prob":   0.70 if confidence == "HIGH" else 0.66,
            "price":      spot,
            "regime":     regime,
            "strategy":   strategy,
            "direction":  s_dir,
            "trade_type": "spread",
            "legs":       spread_legs,
            "auto":       True,
            "source":     "gex_scanner",
        }

    if len(legs) == 4:
        # True Iron Condor — submit as single atomic 4-leg mleg order.
        # Alpaca fills all 4 legs atomically; no partial-fill naked risk.
        call_legs = [l for l in legs if l.get("type","").upper() == "CALL"]
        put_legs  = [l for l in legs if l.get("type","").upper() == "PUT"]

        put_sell  = next((l for l in put_legs  if l.get("action","").upper()=="SELL"), None)
        put_buy   = next((l for l in put_legs  if l.get("action","").upper()=="BUY"),  None)
        call_sell = next((l for l in call_legs if l.get("action","").upper()=="SELL"), None)
        call_buy  = next((l for l in call_legs if l.get("action","").upper()=="BUY"),  None)

        if not all([put_sell, put_buy, call_sell, call_buy]):
            log.info("[%s] Skipping IC — could not extract all 4 legs cleanly", ticker)
            return []

        dte = int(put_sell.get("expiry_dte") or call_sell.get("expiry_dte") or 21)
        log.info(
            "[%s] IC → single atomic order: "
            "BUY PUT $%s / SELL PUT $%s / SELL CALL $%s / BUY CALL $%s  DTE=%d",
            ticker,
            put_buy.get("strike"),  put_sell.get("strike"),
            call_sell.get("strike"), call_buy.get("strike"), dte
        )
        return [{
            "ticker":            ticker,
            "signal":            "HOLD",
            "xgb_prob":          0.70 if confidence == "HIGH" else 0.66,
            "price":             spot,
            "regime":            regime,
            "strategy":          strategy,
            "direction":         "neutral",
            "trade_type":        "iron_condor",
            "legs":              legs,
            "put_short_strike":  float(put_sell.get("strike")),
            "put_long_strike":   float(put_buy.get("strike")),
            "call_short_strike": float(call_sell.get("strike")),
            "call_long_strike":  float(call_buy.get("strike")),
            "expiry_dte":        dte,
            "auto":              True,
            "source":            "gex_scanner",
        }]

    if len(legs) != 2:
        log.info("[%s] Skipping %s — %d legs (need 2)", ticker, strategy, len(legs))
        return []

    log.info("[%s] Signal mapped: %s %s %s", ticker, strategy, direction, confidence)
    return [_make_spread_signal(legs, direction)]


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------

async def run_scan(
    universe:    str  = "ndx100",
    execute:     bool = AUTO_EXECUTE,
    top_n:       int  = TOP_N,
    max_dte:     int  = MAX_DTE,
    trader             = None,       # AlpacaOptionsTrader instance, or None
) -> ScanResult:
    """
    Full pipeline:
      1. Parallel bootstrap of all universe tickers
      2. Screen by GEX criteria
      3. Generate trade ideas (Claude) for top_n candidates
      4. Optionally auto-execute via Alpaca

    Args:
        universe  : "ndx100" or "sp100"
        execute   : if True and trader is provided, auto-execute qualifying ideas
        top_n     : max candidates to generate ideas for
        max_dte   : max days to expiry for options in bootstrap
        trader    : AlpacaOptionsTrader instance (required for execution)
    """
    tickers   = UNIVERSES.get(universe.lower(), NDX100)
    started   = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
    result    = ScanResult(
        universe=universe, started_at=started, finished_at="",
        total_tickers=len(tickers), loaded=0, screened=0,
    )

    log.info("=== GEX SCAN: %s — %d tickers, max_dte=%d ===",
             universe.upper(), len(tickers), max_dte)

    # ── Step 1: Parallel bootstrap ────────────────────────────────────────────
    bootstrap_results: List[TickerGEX] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS,
                            thread_name_prefix="gex-scan") as pool:
        futures = {
            pool.submit(_bootstrap_one, ticker, 0, max_dte): ticker
            for ticker in tickers
        }
        for fut in as_completed(futures):
            ticker = futures[fut]
            try:
                r = fut.result(timeout=120)
                bootstrap_results.append(r)
                if r.error:
                    result.errors.append(f"{ticker}: {r.error}")
                    log.debug("[%s] bootstrap error: %s", ticker, r.error)
                else:
                    result.loaded += 1
                    log.info("[%s] loaded — spot=%.2f contracts=%d GEX=%.3fB regime=%s",
                             ticker, r.spot, r.contracts, r.net_gex_b, r.regime)
            except Exception as e:
                result.errors.append(f"{ticker}: {e}")
                log.warning("[%s] future error: %s", ticker, e)

    log.info("Bootstrap complete: %d/%d loaded in %.0fs",
             result.loaded, len(tickers), time.time() - t0)

    # ── Step 2: Screen candidates ─────────────────────────────────────────────
    candidates = screen_candidates(bootstrap_results)
    result.screened    = len(candidates)
    result.candidates  = candidates

    if not candidates:
        log.warning("No candidates passed screening — scan complete with 0 ideas.")
        result.finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
        return result

    # Log top candidates
    log.info("Top %d candidates:", min(top_n, len(candidates)))
    for i, c in enumerate(candidates[:top_n], 1):
        log.info(
            "  %2d. %-6s  spot=$%-8.2f  regime=%-8s  strength=%.3f  "
            "dist_flip=%s  slope=%-10s  momentum=%-12s  score=%.3f",
            i, c.ticker, c.spot, c.regime, c.strength,
            f"{c.dist_to_flip:+.2f}%" if c.dist_to_flip is not None else "   N/A  ",
            c.slope_regime, c.momentum, c.score,
        )

    # ── Step 3: Generate trade ideas ─────────────────────────────────────────
    from ai_client_anth import generate_trade_ideas

    trade_ideas_all: List[dict] = []
    trades_executed = 0

    for cand in candidates[:top_n]:
        if execute and trader and trades_executed >= MAX_TRADES:
            log.info("Reached MAX_TRADES (%d) — skipping execution for remaining tickers "
                     "(ideas still generated).", MAX_TRADES)


        log.info("[%s] Generating trade ideas…", cand.ticker)
        try:
            gex_data = _candidate_to_gex_data(cand)
            ideas_result = await generate_trade_ideas(
                gex_data  = gex_data,
                alerts    = [],
                ticker    = cand.ticker,
                force     = True,      # always fresh for scanner
            )
            ideas = ideas_result.get("ideas") or []
            if not ideas:
                log.info("[%s] No ideas generated", cand.ticker)
                continue

            for idea in ideas:
                idea["ticker"]    = cand.ticker
                idea["scan_score"] = cand.score
                idea["spot"]      = cand.spot
                idea["gex_regime"] = cand.regime
                idea["gex_strength"] = cand.strength

            trade_ideas_all.extend(ideas)
            log.info("[%s] %d ideas generated (best: %s, %s)",
                     cand.ticker, len(ideas),
                     ideas[0].get("strategy","?"), ideas[0].get("confidence","?"))

            # ── Step 4: Execute ───────────────────────────────────────────────
            if execute and trader and trades_executed < MAX_TRADES:
                for idea in ideas:
                    signals = idea_to_alpaca_signal(
                        idea, cand.ticker, cand.spot, cand.regime,
                        cand=cand)
                    if not signals:
                        continue

                    for signal in signals:
                        try:
                            trade_type = signal.get("trade_type", "spread")
                            meta = {
                                "regime":   signal["regime"],
                                "auto":     True,
                                "strategy": signal.get("strategy"),
                                "source":   "gex_scanner",
                            }
                            if trade_type == "iron_condor":
                                await trader.place_iron_condor(
                                    underlying        = signal["ticker"],
                                    underlying_price  = signal["price"],
                                    put_short_strike  = signal["put_short_strike"],
                                    put_long_strike   = signal["put_long_strike"],
                                    call_short_strike = signal["call_short_strike"],
                                    call_long_strike  = signal["call_long_strike"],
                                    expiry_dte        = signal["expiry_dte"],
                                    qty               = 1,
                                    signal_meta       = meta,
                                )
                            elif trade_type == "single_leg":
                                await trader.place_single_leg(
                                    underlying       = signal["ticker"],
                                    option_type      = signal["option_type"],
                                    side             = signal["side"],
                                    underlying_price = signal["price"],
                                    qty              = 1,
                                    strike           = signal.get("strike"),
                                    expiry_days      = signal.get("expiry_dte", 14),
                                    signal_meta      = meta,
                                )
                            else:
                                # Bull Put or Bear Call spread — direct call, no inspect shim
                                direction = signal.get("direction", "bull")
                                legs      = signal.get("legs") or []
                                short_leg = next((l for l in legs
                                    if l.get("action","").upper()=="SELL"), None)
                                long_leg  = next((l for l in legs
                                    if l.get("action","").upper()=="BUY"),  None)
                                await trader.place_credit_spread(
                                    underlying           = signal["ticker"],
                                    direction            = direction,
                                    underlying_price     = signal["price"],
                                    qty                  = 1,
                                    short_strike         = float(short_leg.get("strike") or 0) or None
                                                          if short_leg else None,
                                    long_strike          = float(long_leg.get("strike") or 0) or None
                                                          if long_leg else None,
                                    expiry_dte           = int(
                                        (short_leg or long_leg or {}).get("expiry_dte") or 30),
                                    signal_meta          = meta,
                                )

                            exec_record = {
                                "ticker":     cand.ticker,
                                "strategy":   (f"{idea.get('strategy')} "
                                               f"({signal.get('direction','?')})"),
                                "trade_type": trade_type,
                                "direction":  signal.get("direction"),
                                "confidence": idea.get("confidence"),
                                "spot":       cand.spot,
                                "ts":         datetime.now(timezone.utc)
                                              .isoformat().replace("+00:00","Z"),
                                "status":     "submitted",
                            }
                            result.executions.append(exec_record)
                            trades_executed += 1
                            log.info("[%s] ✓ %s %s submitted",
                                     cand.ticker, idea.get("strategy"),
                                     signal.get("direction",""))
                        except Exception as e:
                            log.warning("[%s] Execution error (%s): %s",
                                        cand.ticker, signal.get("direction","?"), e)
                            result.executions.append({
                                "ticker":     cand.ticker,
                                "strategy":   idea.get("strategy"),
                                "trade_type": signal.get("trade_type","spread"),
                                "direction":  signal.get("direction"),
                                "error":      str(e),
                                "ts":         datetime.now(timezone.utc)
                                              .isoformat().replace("+00:00","Z"),
                                "status":     "failed",
                            })

        except Exception as e:
            log.error("[%s] generate_trade_ideas error: %s", cand.ticker, e)
            result.errors.append(f"{cand.ticker} ideas: {e}")

    result.trade_ideas  = trade_ideas_all
    result.finished_at  = datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

    # ── Persist ideas to SQLite ───────────────────────────────────────────────
    if trade_ideas_all:
        try:
            from gex_core import SnapshotStore
            import os
            db_path = os.getenv("GEX_DB_PATH", "gex_snapshots.db")
            store   = SnapshotStore(db_path)
            by_ticker: dict = {}
            for idea in trade_ideas_all:
                t = idea.get("ticker", "")
                if t:
                    by_ticker.setdefault(t, []).append(idea)
            saved = 0
            for t, t_ideas in by_ticker.items():
                cand   = next((c for c in result.candidates if c.ticker == t), None)
                saved += store.insert_trade_ideas(
                    underlying = t,
                    ideas      = t_ideas,
                    spot       = cand.spot   if cand else 0,
                    regime     = cand.regime if cand else "",
                    source     = "scanner",
                )
            log.info("Persisted %d new ideas to DB from scanner run", saved)
        except Exception as e:
            log.warning("Scanner DB persist failed: %s", e)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = (datetime.fromisoformat(result.finished_at.rstrip("Z")) -
               datetime.fromisoformat(result.started_at.rstrip("Z"))).total_seconds()
    log.info(
        "=== SCAN COMPLETE: %s | loaded=%d screened=%d ideas=%d executed=%d "
        "errors=%d elapsed=%.0fs ===",
        universe.upper(), result.loaded, result.screened,
        len(result.trade_ideas), len(result.executions),
        len(result.errors), elapsed,
    )
    return result


# ---------------------------------------------------------------------------
# FastAPI integration — drop these into gex_server.py
# ---------------------------------------------------------------------------

def register_scan_routes(app, engines: dict, state_cache: dict):
    """
    Call this from gex_server.py lifespan to add scanner endpoints.

    Usage in gex_server.py:
        from gex_scanner import register_scan_routes
        register_scan_routes(app, _engines, _state_cache)
    """
    import asyncio
    from fastapi import BackgroundTasks
    from fastapi.responses import JSONResponse

    _scan_state: dict = {"running": False, "last_result": None}

    def _result_to_dict(r: ScanResult) -> dict:
        return {
            "universe":      r.universe,
            "started_at":    r.started_at,
            "finished_at":   r.finished_at,
            "total_tickers": r.total_tickers,
            "loaded":        r.loaded,
            "screened":      r.screened,
            "candidates": [
                {
                    "ticker":       c.ticker,
                    "spot":         c.spot,
                    "contracts":    c.contracts,
                    "regime":       c.regime,
                    "strength":     c.strength,
                    "net_gex_b":    c.net_gex_b,
                    "zero_gamma":   c.zero_gamma,
                    "call_wall":    c.call_wall,
                    "put_wall":     c.put_wall,
                    "dist_to_flip": c.dist_to_flip,
                    "slope_regime": c.slope_regime,
                    "momentum":     c.momentum,
                    "score":        c.score,
                }
                for c in r.candidates
            ],
            "trade_ideas":  r.trade_ideas,
            "executions":   r.executions,
            "error_count":  len(r.errors),
            "errors":       r.errors[:20],
        }

    @app.post("/api/scan")
    async def trigger_scan(
        background_tasks: BackgroundTasks,
        universe:  str  = "ndx100",
        execute:   bool = False,
        top_n:     int  = TOP_N,
        max_dte:   int  = MAX_DTE,
    ):
        """
        Launch a GEX scan in the background.

        universe : "ndx100" or "sp100"
        execute  : if true, auto-execute qualifying ideas via Alpaca
        top_n    : max tickers to generate ideas for (default 15)
        max_dte  : options DTE window for bootstrap (default 14)
        """
        if universe.lower() not in UNIVERSES:
            return JSONResponse(
                {"error": f"Unknown universe '{universe}'. Choose: {list(UNIVERSES)}"}, 400)

        if _scan_state["running"]:
            return JSONResponse({"status": "already_running",
                                  "message": "A scan is in progress. Check /api/scan/status."})

        trader = None
        if execute:
            try:
                from alpaca_options_trader import AlpacaOptionsTrader
                trader = AlpacaOptionsTrader()
            except Exception as e:
                return JSONResponse(
                    {"error": f"Cannot init Alpaca trader: {e}"}, 500)

        async def _run():
            _scan_state["running"] = True
            try:
                result = await run_scan(
                    universe=universe, execute=execute,
                    top_n=top_n, max_dte=max_dte, trader=trader,
                )
                _scan_state["last_result"] = result
            finally:
                _scan_state["running"] = False

        background_tasks.add_task(_run)
        return {
            "status":   "started",
            "universe": universe.upper(),
            "tickers":  len(UNIVERSES[universe.lower()]),
            "execute":  execute,
            "top_n":    top_n,
            "max_dte":  max_dte,
            "poll":     "/api/scan/status",
        }

    @app.get("/api/scan/status")
    async def scan_status():
        """Poll this to check if scan is running and see latest results."""
        r = _scan_state["last_result"]
        return {
            "running":      _scan_state["running"],
            "has_result":   r is not None,
            "last_scan": {
                "universe":    r.universe,
                "started_at":  r.started_at,
                "finished_at": r.finished_at,
                "loaded":      r.loaded,
                "screened":    r.screened,
                "ideas":       len(r.trade_ideas),
                "executions":  len(r.executions),
            } if r else None,
        }

    @app.get("/api/scan/results")
    async def scan_results():
        """Full results of the last completed scan."""
        r = _scan_state["last_result"]
        if not r:
            return JSONResponse({"error": "No scan results yet. POST /api/scan first."}, 404)
        return _result_to_dict(r)

    @app.get("/api/scan/ideas")
    async def scan_ideas(min_confidence: str = "", ticker: str = ""):
        """Trade ideas from last scan, optionally filtered."""
        r = _scan_state["last_result"]
        if not r:
            return JSONResponse({"error": "No scan results yet."}, 404)
        ideas = r.trade_ideas
        if ticker:
            ideas = [i for i in ideas if i.get("ticker","").upper() == ticker.upper()]
        if min_confidence:
            rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
            min_r = rank.get(min_confidence.upper(), 0)
            ideas = [i for i in ideas if rank.get((i.get("confidence") or "").upper(), 0) >= min_r]
        return {"count": len(ideas), "ideas": ideas}

    log.info("Scanner routes registered: POST /api/scan, GET /api/scan/status|results|ideas")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def _cli_main():
    import argparse
    parser = argparse.ArgumentParser(description="GEX Scanner — NDX100 / SP100")
    parser.add_argument("--universe",  default="ndx100",
                        choices=["ndx100", "sp100"],
                        help="Ticker universe to scan")
    parser.add_argument("--execute",   action="store_true",
                        help="Auto-execute qualifying trades via Alpaca")
    parser.add_argument("--top",       type=int, default=TOP_N,
                        help=f"Max candidates to generate ideas for (default {TOP_N})")
    parser.add_argument("--max-dte",   type=int, default=MAX_DTE,
                        help=f"Max DTE for options bootstrap (default {MAX_DTE})")
    parser.add_argument("--output",    default="",
                        help="Save results to JSON file")
    args = parser.parse_args()

    trader = None
    if args.execute:
        from alpaca_options_trader import AlpacaOptionsTrader
        trader = AlpacaOptionsTrader()
        log.info("Alpaca trader initialised — %s",
                 "PAPER" if trader.is_paper else "LIVE")

    result = await run_scan(
        universe = args.universe,
        execute  = args.execute,
        top_n    = args.top,
        max_dte  = args.max_dte,
        trader   = trader,
    )

    # Print summary table
    print("\n" + "=" * 78)
    print(f"  GEX SCAN RESULTS — {args.universe.upper()}  "
          f"({result.screened} candidates from {result.loaded} loaded)")
    print("=" * 78)
    print(f"  {'Ticker':<7} {'Spot':>8}  {'Regime':<9} {'Strength':>8} "
          f"{'Dist%':>7} {'Slope':<12} {'Mom':<13} {'Score':>6}")
    print("  " + "-" * 74)
    for c in result.candidates[:args.top]:
        dist = f"{c.dist_to_flip:+.2f}" if c.dist_to_flip is not None else "  N/A"
        print(f"  {c.ticker:<7} ${c.spot:>8.2f}  {c.regime:<9} {c.strength:>8.3f} "
              f"{dist:>7}  {c.slope_regime:<12} {c.momentum:<13} {c.score:>6.3f}")

    if result.trade_ideas:
        print(f"\n  Trade Ideas ({len(result.trade_ideas)}):")
        print("  " + "-" * 74)
        for idea in result.trade_ideas:
            conf  = idea.get("confidence","?")
            strat = idea.get("strategy","?")
            dirn  = idea.get("direction","?")
            tick  = idea.get("ticker","?")
            rr    = idea.get("risk_reward","?")
            print(f"  {tick:<7} {conf:<8} {dirn:<10} {strat:<30} RR: {rr}")

    if result.executions:
        print(f"\n  Executions ({len(result.executions)}):")
        for ex in result.executions:
            status = ex.get("status","?")
            print(f"  {ex.get('ticker','?'):<7} {ex.get('strategy','?'):<25} "
                  f"{ex.get('direction','?'):<10} {status}")

    if result.errors:
        print(f"\n  Errors ({len(result.errors)}): {result.errors[:5]}")

    print("=" * 78 + "\n")

    if args.output:
        import dataclasses

        def _serialise(obj):
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)
            raise TypeError(f"Not serialisable: {type(obj)}")

        with open(args.output, "w") as f:
            json.dump(dataclasses.asdict(result), f, indent=2, default=str)
        log.info("Results saved to %s", args.output)


if __name__ == "__main__":
    asyncio.run(_cli_main())
