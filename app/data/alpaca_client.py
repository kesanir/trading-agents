"""
alpaca_client.py — Alpaca Market Data API layer
================================================
Single source of truth for all Alpaca HTTP calls.

Replaces polygon_client.py for equities and ETFs (QQQ, SPY, AAPL, etc.).
Cash indices (SPX, NDX, RUT) are not supported by Alpaca — use a separate
source if needed, or proxy via SPY/QQQ.

Provides:
  get_spot(ticker)                    → float
  get_ohlc(ticker, res_min, hours)    → list[dict]
  iter_options_chain(ticker, ...)     → Iterator[dict]   (normalized to gex_core schema)
  compute_iv_from_quote(item, S)      → float            (Newton-Raphson, same as before)

Alpaca endpoints used:
  ✓  GET /v2/stocks/{ticker}/trades/latest         — spot price
  ✓  GET /v2/stocks/{ticker}/bars?timeframe=1Min   — OHLC bars
  ✓  GET /v1beta1/options/chain                    — greeks + IV + bid/ask per contract
  ✓  GET /v2/assets (trading API)                  — OI fallback if chain omits it

Feed: "indicative" (free, ~15min delayed) — sufficient for GEX computation.
      Set ALPACA_OPTION_FEED=opra in .env for real-time OPRA data (paid).

Known limitations:
  - 0DTE options: Alpaca returns no greeks (T=0 → BS divide-by-zero).
    gex_core._bs_gamma() + _DEFAULT_IV handles these as before.
  - OI in the chain response is T+1 (previous day's settlement).
    This matches Polygon Starter behavior — no regression.
  - Rate limit: 1000 req/min on free tier. iter_options_chain() paces
    at 0.1s between pages; concurrent bootstraps share the limit.
"""

from __future__ import annotations

import logging
import math as _math
import os
import time
from datetime import datetime, date, timedelta
from typing import Dict, Iterator, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("gex")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
OPTION_FEED   = os.getenv("ALPACA_OPTION_FEED", "indicative")   # or "opra"
STOCK_FEED    = os.getenv("ALPACA_STOCK_FEED",  "iex")          # or "sip"

DATA_URL    = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"   # paper; swap for live

CONTRACT_SIZE = 100

if not ALPACA_KEY or not ALPACA_SECRET:
    raise ValueError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")

_AUTH_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

# ---------------------------------------------------------------------------
# Thread-local session — same pattern as polygon_client
# ---------------------------------------------------------------------------

import threading as _threading
_local = _threading.local()


def _make_session() -> requests.Session:
    s     = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8))
    s.headers.update(_AUTH_HEADERS)
    return s


def _session() -> requests.Session:
    if not hasattr(_local, "session"):
        _local.session = _make_session()
    return _local.session


def _get(url: str, params: dict = None, base: str = DATA_URL,
         log_errors: bool = False) -> Optional[dict]:
    """Authenticated GET. Returns parsed JSON or None on any error."""
    full = (base + url) if url.startswith("/") else url
    try:
        r = _session().get(full, params=params or {}, timeout=15)
        if r.status_code == 200:
            return r.json()
        if log_errors:
            log.warning("[alpaca] HTTP %d → %s", r.status_code, full.split("?")[0])
        else:
            log.debug("[alpaca] HTTP %d → %s", r.status_code, full.split("?")[0])
        return None
    except Exception as e:
        log.debug("[alpaca] error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Market hours  (reused from polygon_client — no external dependency)
# ---------------------------------------------------------------------------

def _et_now() -> datetime:
    """Current datetime in US/Eastern, DST-aware."""
    from datetime import timezone
    utc_now   = datetime.now(timezone.utc)
    yr        = utc_now.year
    mar1      = date(yr, 3, 1)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1      = date(yr, 11, 1)
    dst_end   = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    is_dst    = dst_start <= utc_now.date() < dst_end
    return utc_now + timedelta(hours=-4 if is_dst else -5)


def _is_market_open() -> bool:
    et = _et_now()
    if et.weekday() >= 5:
        return False
    t = et.hour * 60 + et.minute
    return 570 <= t <= 960   # 09:30–16:00


def _last_trading_day() -> date:
    d = _et_now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# Spot price
# ---------------------------------------------------------------------------

_YF_INDEX_MAP: Dict[str, str] = {
    "SPX": "^GSPC",
    "NDX": "^NDX",
    "RUT": "^RUT",
    "VIX": "^VIX",
    "DJI": "^DJI",
}

_yf_lock = _threading.Lock()


def _get_spot_yfinance(ticker: str) -> float:
    """Fetch spot via yfinance for cash indices (SPX, NDX, etc.)."""
    yf_sym = _YF_INDEX_MAP.get(ticker.upper(), ticker.upper())
    try:
        import yfinance as yf
        with _yf_lock:
            t = yf.Ticker(yf_sym)
            p = getattr(t.fast_info, "last_price", None)
            if not (p and p > 0):
                p = t.info.get("regularMarketPrice") or t.info.get("currentPrice")
        if p and p > 0:
            log.info("[spot] %s = %.2f via yfinance (%s)", ticker, float(p), yf_sym)
            return float(p)
    except ImportError:
        log.debug("[spot] yfinance not installed")
    except Exception as e:
        log.debug("[spot] yfinance error for %s (%s): %s", ticker, yf_sym, e)
    return 0.0


def get_spot(ticker: str) -> float:
    """
    Best available spot price for an equity, ETF, or cash index.

    Hierarchy:
      1. Latest trade  — real-time during market hours
      2. Latest bar    — 1-min bar, slightly lagged
      3. Previous bar  — prev session close
      4. yfinance      — fallback for cash indices (SPX, NDX, RUT)
    """
    ticker = ticker.upper()

    # Cash indices — skip Alpaca equity endpoints, go straight to yfinance
    if ticker in _YF_INDEX_MAP:
        return _get_spot_yfinance(ticker)

    # Latest trade — most current
    d = _get(f"/v2/stocks/{ticker}/trades/latest",
             {"feed": STOCK_FEED}, log_errors=True)
    if d:
        p = float((d.get("trade") or {}).get("p") or 0)
        if p > 0:
            log.info("[spot] %s = %.2f via Alpaca latest trade", ticker, p)
            return p

    # Latest 1-min bar
    d = _get(f"/v2/stocks/{ticker}/bars/latest",
             {"timeframe": "1Min", "feed": STOCK_FEED}, log_errors=True)
    if d:
        p = float((d.get("bar") or {}).get("c") or 0)
        if p > 0:
            log.info("[spot] %s = %.2f via Alpaca latest bar", ticker, p)
            return p

    # Previous close — daily bar
    from datetime import timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    ago_ms = now_ms - 5 * 24 * 3600 * 1000
    d = _get(
        f"/v2/stocks/{ticker}/bars",
        {"timeframe": "1Day", "start": datetime.utcfromtimestamp(ago_ms / 1000).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "limit": 1, "sort": "desc", "feed": STOCK_FEED},
        log_errors=True,
    )
    if d:
        bars = d.get("bars") or []
        if bars:
            p = float(bars[0].get("c") or 0)
            if p > 0:
                log.info("[spot] %s = %.2f via Alpaca daily bar", ticker, p)
                return p

    log.warning("[spot] %s all Alpaca spot sources returned 0", ticker)
    return 0.0


def get_ohlc(ticker: str, resolution_min: int = 5, hours: float = 6) -> List[dict]:
    """
    OHLC bars for the GEX dashboard candlestick chart.

    resolution_min : snapped to [1, 2, 5, 15, 30, 60]
    hours          : lookback in hours

    Returns list of {t, o, h, l, c, v} sorted ascending.
    """
    ticker = ticker.upper()
    valid  = [1, 2, 5, 15, 30, 60]
    res    = min(valid, key=lambda x: abs(x - int(resolution_min)))

    from datetime import timezone
    now_dt  = datetime.now(timezone.utc)
    ago_dt  = now_dt - timedelta(hours=hours)

    tf_map  = {1: "1Min", 2: "2Min", 5: "5Min", 15: "15Min", 30: "30Min", 60: "1Hour"}
    tf      = tf_map.get(res, "5Min")

    d = _get(
        f"/v2/stocks/{ticker}/bars",
        {
            "timeframe": tf,
            "start":     ago_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":       now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":     500,
            "sort":      "asc",
            "feed":      STOCK_FEED,
        },
    )
    if not d:
        return []

    bars = [
        {
            "t": int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp() * 1000),
            "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
            "v": round(b.get("v", 0), 0),
        }
        for b in (d.get("bars") or [])
        if all(k in b for k in ("t", "o", "h", "l", "c"))
    ]
    log.debug("[ohlc] %s %s × %d bars", ticker, tf, len(bars))
    return bars


# ---------------------------------------------------------------------------
# Symbol parsing  — Alpaca option symbols follow OCC format:
#   ROOT + YYMMDD + C/P + 8-digit strike (5 integer + 3 decimal, no dot)
#   e.g. AAPL240119C00100000 → AAPL, 2024-01-19, call, $100.00
# ---------------------------------------------------------------------------

def _parse_option_symbol(sym: str) -> Optional[dict]:
    """
    Parse OCC option symbol into components.
    Returns None if symbol doesn't match expected format.
    """
    import re
    m = re.match(
        r'^([A-Z]{1,6})(\d{6})([CP])(\d{8})$', sym.upper().replace(" ", "")
    )
    if not m:
        return None
    root, ymd, cp, strike_raw = m.groups()
    try:
        exp_date = datetime.strptime(ymd, "%y%m%d").date().isoformat()
        strike   = int(strike_raw) / 1000.0
        ctype    = "call" if cp == "C" else "put"
        return {
            "ticker":          sym,
            "underlying":      root,
            "expiration_date": exp_date,
            "contract_type":   ctype,
            "strike_price":    strike,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Options chain — normalized to gex_core schema
# ---------------------------------------------------------------------------

def fetch_oi_map(ticker: str, min_dte: int = 0, max_dte: int = 45) -> Dict[str, int]:
    """
    Fetch open interest for all option contracts of a ticker via the
    Alpaca Trading API (GET /v2/options/contracts).

    IMPORTANT: by default this endpoint only returns contracts expiring
    before the upcoming weekend. Must pass expiration_date_lte to get
    the full DTE range matching the snapshot.

    Returns {occ_symbol: open_interest} — T+1 settlement, same as Polygon.
    Returns empty dict on failure (GEX will still compute but walls will be weak).
    """
    ticker  = ticker.upper()
    today   = date.today()
    exp_gte = (today + timedelta(days=min_dte)).isoformat()
    exp_lte = (today + timedelta(days=max_dte)).isoformat()
    oi_map: Dict[str, int] = {}
    url    = "/v2/options/contracts"
    params = {
        "underlying_symbols":   ticker,
        "status":               "active",
        "expiration_date_gte":  exp_gte,
        "expiration_date_lte":  exp_lte,
        "limit":                1000,
    }

    while url:
        d = _get(url, params, base=TRADING_URL, log_errors=True)
        if not d:
            log.warning("[alpaca] OI fetch failed for %s", ticker)
            break

        for c in (d.get("option_contracts") or []):
            sym = c.get("symbol")
            oi  = c.get("open_interest")
            # Guard against API returning contracts for other tickers
            # (underlying_symbols filter is not always exact on the trading API)
            if sym and oi is not None and sym.startswith(ticker):
                oi_map[sym] = int(float(oi))

        next_token = d.get("next_page_token")
        if next_token:
            url    = "/v2/options/contracts"
            # Keep all filters on subsequent pages — the trading API pagination
            # token does NOT carry underlying_symbols or expiry filters server-side
            params = {
                "underlying_symbols":   ticker,
                "status":               "active",
                "expiration_date_gte":  exp_gte,
                "expiration_date_lte":  exp_lte,
                "page_token":           next_token,
                "limit":                1000,
            }
        else:
            break

    log.info("[alpaca] OI map for %s (DTE %d-%d): %d contracts", ticker, min_dte, max_dte, len(oi_map))
    return oi_map


def iter_options_chain(
    ticker:  str,
    min_dte: int = 0,
    max_dte: int = 45,
    limit:   int = 1000,
    oi_map:  Optional[Dict[str, int]] = None,
) -> Iterator[dict]:
    """
    Paginate Alpaca /v1beta1/options/chain for one underlying.

    Yields one dict per contract, normalized to match the schema
    gex_core.fast_bootstrap() expects from iter_options_snapshot():

        {
            "details": {
                "ticker":           str,   # OCC symbol e.g. AAPL240119C00100000
                "expiration_date":  str,   # "YYYY-MM-DD"
                "contract_type":    str,   # "call" | "put"
                "strike_price":     float,
            },
            "greeks":            {"gamma": float, ...} | {},
            "implied_volatility": float | None,
            "open_interest":      int,
            "last_quote": {
                "bid": float,
                "ask": float,
            },
            "day": {
                "volume": int,
                "vwap":   float,
            },
            # No underlying_asset.price — spot seeded separately via get_spot()
        }

    0DTE contracts are included but will have no greeks (Alpaca limitation).
    gex_core handles these via _bs_gamma() + _DEFAULT_IV fallback.

    Pagination: Alpaca uses next_page_token cursor, limit max 1000/page.
    Rate limit:  1000 req/min — 0.1s sleep between pages is conservative.
    """
    ticker  = ticker.upper()
    today   = date.today()
    exp_gte = (today + timedelta(days=min_dte)).isoformat()
    exp_lte = (today + timedelta(days=max_dte)).isoformat()

    # Correct endpoint: /v1beta1/options/snapshots/{underlying_symbol}
    # (not /options/chain — that's the docs label, not the URL path)
    url    = f"/v1beta1/options/snapshots/{ticker}"
    params = {
        "feed":                 OPTION_FEED,
        "limit":                limit,
        "expiration_date_gte":  exp_gte,
        "expiration_date_lte":  exp_lte,
    }
    page = 0

    while url:
        d = _get(url, params, log_errors=True)
        if d is None:
            log.error("[alpaca] options chain page %d failed for %s", page + 1, ticker)
            return

        page    += 1
        # Response key is "snapshots" — a dict of {symbol: snapshot_data}
        chain    = d.get("snapshots") or {}

        for sym, snap in chain.items():
            parsed = _parse_option_symbol(sym)
            if not parsed:
                continue

            greeks_raw = snap.get("greeks") or {}
            greeks = {
                "gamma": float(greeks_raw.get("gamma") or 0),
                "delta": float(greeks_raw.get("delta") or 0),
                "theta": float(greeks_raw.get("theta") or 0),
                "vega":  float(greeks_raw.get("vega")  or 0),
                "rho":   float(greeks_raw.get("rho")   or 0),
            } if greeks_raw else {}

            lq  = snap.get("latestQuote") or {}
            lt  = snap.get("latestTrade") or {}

            yield {
                "details": {
                    "ticker":          sym,
                    "expiration_date": parsed["expiration_date"],
                    "contract_type":   parsed["contract_type"],
                    "strike_price":    parsed["strike_price"],
                },
                "greeks":             greeks,
                "implied_volatility": float(snap.get("impliedVolatility") or 0) or None,
                "open_interest":      (oi_map or {}).get(sym) if oi_map is not None
                                      else int(snap.get("openInterest") or 0),
                "last_quote": {
                    "bid": float(lq.get("bp") or lq.get("bid") or 0),
                    "ask": float(lq.get("ap") or lq.get("ask") or 0),
                },
                "day": {
                    "volume": int(lt.get("s") or 0),
                    "vwap":   float(lt.get("p") or 0),
                },
            }

        next_token = d.get("next_page_token")
        if next_token:
            url    = f"/v1beta1/options/snapshots/{ticker}"
            params = {
                "feed":                OPTION_FEED,
                "limit":               limit,
                "expiration_date_gte": exp_gte,
                "expiration_date_lte": exp_lte,
                "page_token":          next_token,
            }
            time.sleep(0.1)
        else:
            break


# ---------------------------------------------------------------------------
# IV from quote — identical logic to polygon_client.compute_iv_from_quote()
# kept here so alpaca_client is self-contained with no polygon dependency
# ---------------------------------------------------------------------------

def compute_iv_from_quote(item: dict, S: float, r: float = 0.0) -> float:
    """
    Newton-Raphson IV from bid/ask mid of a chain item.

    Same algorithm as polygon_client.compute_iv_from_quote() —
    gex_core._resolve_iv() calls this as step 5 fallback.

    Mid-price sources (in order):
      1. bid/ask mid from last_quote
      2. day.vwap (latest trade price)
    """
    det   = item.get("details") or {}
    try:
        exp = date.fromisoformat(det.get("expiration_date", ""))
        T   = max((exp - date.today()).days, 0) / 365.0
    except Exception:
        return 0.0
    if T <= 0:
        return 0.0

    K     = float(det.get("strike_price") or 0)
    ctype = str(det.get("contract_type") or "").lower()
    if K <= 0 or S <= 0 or ctype not in ("call", "put"):
        return 0.0

    moneyness = abs(S - K) / S
    if ctype == "call" and K < S * 0.80:
        return 0.0
    if ctype == "put"  and K > S * 1.20:
        return 0.0

    lq  = item.get("last_quote") or {}
    bid = float(lq.get("bid") or 0)
    ask = float(lq.get("ask") or 0)

    if bid > 0 and ask > 0 and ask >= bid:
        mid     = (bid + ask) / 2.0
        mid_src = "bid_ask"
    elif ask > 0:
        mid     = ask
        mid_src = "ask_only"
    else:
        mid     = float((item.get("day") or {}).get("vwap") or 0)
        mid_src = "day_vwap"

    if mid <= 0:
        return 0.0
    item["_iv_mid_src"] = mid_src

    import math as _m
    disc      = _m.exp(-r * T)
    F         = S / disc
    intrinsic = max(0.0, (F - K) * disc) if ctype == "call" else max(0.0, (K - F) * disc)
    if mid <= intrinsic + 1e-6:
        return 0.0
    time_value = mid - intrinsic
    if mid > 0 and time_value < mid * 0.01:
        return 0.0

    def _norm_cdf(x: float) -> float:
        t = 1.0 / (1.0 + 0.2316419 * abs(x))
        poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937
               + t * (-1.821255978 + t * 1.330274429))))
        p = 1.0 - _m.exp(-0.5 * x * x) / _m.sqrt(2 * _m.pi) * poly
        return p if x >= 0 else 1.0 - p

    def _bs_price(sigma: float) -> float:
        if sigma <= 0:
            return intrinsic
        sqT = _m.sqrt(T)
        d1  = (_m.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
        d2  = d1 - sigma * sqT
        if ctype == "call":
            return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
        else:
            return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)

    def _bs_vega(sigma: float) -> float:
        if sigma <= 1e-6:
            return 0.0
        sqT = _m.sqrt(T)
        d1  = (_m.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
        return S * _m.exp(-0.5 * d1 * d1) / _m.sqrt(2 * _m.pi) * sqT

    MAX_SIGMA = 4.0
    sigma     = _m.sqrt(2 * _m.pi / T) * mid / S
    sigma     = min(sigma, MAX_SIGMA)

    for _ in range(50):
        price = _bs_price(sigma)
        vega  = _bs_vega(sigma)
        if vega < 1e-10:
            break
        diff  = price - mid
        sigma -= diff / vega
        sigma  = max(sigma, 1e-6)
        sigma  = min(sigma, MAX_SIGMA)
        if abs(diff) < 1e-6:
            return round(sigma, 4) if 0.01 <= sigma <= MAX_SIGMA else 0.0

    lo, hi = 0.001, MAX_SIGMA
    if (_bs_price(lo) - mid) * (_bs_price(hi) - mid) > 0:
        return 0.0
    for _ in range(100):
        mid_s = (lo + hi) / 2.0
        if hi - lo < 1e-5:
            return round(mid_s, 4) if 0.01 <= mid_s <= MAX_SIGMA else 0.0
        if (_bs_price(mid_s) - mid) * (_bs_price(lo) - mid) < 0:
            hi = mid_s
        else:
            lo = mid_s
    return 0.0



