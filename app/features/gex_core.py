"""
gex_core.py — GEX computation engine
======================================
Pure computation and persistence.  No HTTP calls — all data retrieval in alpaca_client.py.

Responsibilities:
  SnapshotStore        — SQLite persistence (snapshots, levels history, alerts)
  GEXEngine            — contract state, GEX-by-strike / bucketed / expiry
  compute_key_levels() — zero gamma, walls, regime, skew
  fast_bootstrap()     — populate engine from options chain snapshot
"""

import json
import logging
import math as _math
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from app.data.alpaca_client import (
    CONTRACT_SIZE,
    _et_now,
    _is_market_open,
    _last_trading_day,
    compute_iv_from_quote,
    fetch_oi_map,
    get_spot,
    get_ohlc,
    iter_options_chain,
)

log = logging.getLogger("gex")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


# ---------------------------------------------------------------------------
# Black-Scholes gamma  — used when Polygon doesn't return greeks
# ---------------------------------------------------------------------------

def _norm_pdf(x: float) -> float:
    """Standard normal PDF — no scipy dependency."""
    return _math.exp(-0.5 * x * x) / _math.sqrt(2.0 * _math.pi)


def _bs_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """
    Black-Scholes gamma for a European option (calls and puts share the same gamma).

    S     — underlying spot
    K     — strike
    T     — time to expiry in years  (0 → expired → returns 0)
    sigma — implied vol, annualised  (e.g. 0.175 = 17.5%)
    r     — risk-free rate (default 0 — negligible for short-dated)

    Returns per-unit gamma; multiply by CONTRACT_SIZE * OI * S² * 0.01 for GEX $$.
    Returns 0.0 on any invalid input.
    """
    try:
        if T <= 0 or sigma <= 0.001 or S <= 0 or K <= 0:
            return 0.0
        d1 = (_math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * _math.sqrt(T))
        return _norm_pdf(d1) / (S * sigma * _math.sqrt(T))
    except Exception:
        return 0.0


# Default annualised IV used when Polygon returns neither greeks nor IV.
# This is the normal case for SPX, NDX, RUT on the Options Starter plan.
# Values are conservative 30-day ATM estimates — good enough for relative GEX.
#
# Runtime override (tracks VIX): GEX_DEFAULT_IV_SPX=0.185 in .env
_DEFAULT_IV: Dict[str, float] = {
    # ── Cash indices ──────────────────────────────────────────────────────────
    # Override at runtime: GEX_DEFAULT_IV_SPX=0.185 (set to VIX/100)
    "SPX":  0.175,   "SPXW": 0.175,   # Weekly SPX = same underlying
    "NDX":  0.195,   "XND":  0.195,
    "RUT":  0.225,   "VIX":  1.100,   "DJI":  0.155,
    # ── Broad ETFs ────────────────────────────────────────────────────────────
    "SPY":  0.165,   "QQQ":  0.190,   "IWM":  0.215,   "DIA":  0.148,
    "GLD":  0.145,   "SLV":  0.260,   "TLT":  0.130,   "HYG":  0.115,
    "XLE":  0.270,   "XLF":  0.215,   "XLK":  0.225,   "XLV":  0.190,
    "XLI":  0.200,   "XLU":  0.175,   "XLP":  0.165,   "XLB":  0.220,
    "XLRE": 0.210,   "XLC":  0.230,   "ARKK": 0.550,
    # ── Semiconductors ────────────────────────────────────────────────────────
    "NVDA": 0.490,   "AMD":  0.510,   "INTC": 0.390,   "QCOM": 0.310,
    "TXN":  0.250,   "AMAT": 0.350,   "LRCX": 0.340,   "KLAC": 0.340,
    "MRVL": 0.430,   "MCHP": 0.300,   "ADI":  0.260,   "NXPI": 0.290,
    "MU":   0.420,   "ASML": 0.340,   "ON":   0.390,   "GFS":  0.350,
    "AVGO": 0.400,
    # ── Mega-cap tech ─────────────────────────────────────────────────────────
    "AAPL": 0.270,   "MSFT": 0.260,   "GOOGL":0.270,   "GOOG": 0.270,
    "AMZN": 0.295,   "META": 0.360,   "TSLA": 0.620,
    # ── Software / cloud ──────────────────────────────────────────────────────
    "INTU": 0.280,   "ADBE": 0.310,   "CRM":  0.330,   "ORCL": 0.270,
    "SNPS": 0.270,   "CDNS": 0.270,   "ANSS": 0.270,   "ADSK": 0.300,
    "WDAY": 0.330,   "NOW":  0.310,   "TEAM": 0.370,   "ZM":   0.450,
    "PANW": 0.350,   "CRWD": 0.410,   "FTNT": 0.300,   "ZS":   0.400,
    "DDOG": 0.430,   "SGEN": 0.320,   "CDW":  0.250,   "CTSH": 0.230,
    # ── Internet / consumer tech ──────────────────────────────────────────────
    "NFLX": 0.380,   "BKNG": 0.280,   "MELI": 0.380,   "EBAY": 0.260,
    "PYPL": 0.380,   "MTCH": 0.420,   "TTD":  0.490,   "TTWO": 0.350,
    "EA":   0.270,   "WBD":  0.430,
    # ── Consumer staples / retail ─────────────────────────────────────────────
    "COST": 0.200,   "WMT":  0.185,   "TGT":  0.290,   "TJX":  0.230,
    "LOW":  0.250,   "HD":   0.240,   "MCD":  0.200,   "SBUX": 0.270,
    "NKE":  0.280,   "MO":   0.195,   "PM":   0.210,   "KO":   0.175,
    "PG":   0.175,   "PEP":  0.175,   "MDLZ": 0.185,   "KHC":  0.210,
    "KDP":  0.190,   "MNST": 0.270,   "ROST": 0.240,   "ORLY": 0.230,
    "LULU": 0.340,   "DLTR": 0.280,   "MAR":  0.260,
    # ── Healthcare / pharma / biotech ─────────────────────────────────────────
    "UNH":  0.220,   "JNJ":  0.185,   "LLY":  0.290,   "MRK":  0.220,
    "ABBV": 0.240,   "ABT":  0.220,   "BMY":  0.240,   "PFE":  0.250,
    "CI":   0.230,   "DHR":  0.230,   "AMGN": 0.230,   "GILD": 0.240,
    "VRTX": 0.270,   "REGN": 0.270,   "BIIB": 0.310,   "MRNA": 0.580,
    "IDXX": 0.260,   "ILMN": 0.360,   "DXCM": 0.380,   "ISRG": 0.270,
    "AZN":  0.240,   "GEHC": 0.280,
    # ── Financials ────────────────────────────────────────────────────────────
    "JPM":  0.240,   "BAC":  0.270,   "GS":   0.260,   "MS":   0.260,
    "WFC":  0.270,   "AXP":  0.255,   "BX":   0.310,   "SCHW": 0.290,
    "USB":  0.250,   "MET":  0.230,   "V":    0.230,   "MA":   0.240,
    "NDAQ": 0.230,   "MSCI": 0.250,
    # ── Industrials / transport ───────────────────────────────────────────────
    "CAT":  0.265,   "DE":   0.265,   "BA":   0.340,   "RTX":  0.230,
    "GE":   0.280,   "ETN":  0.250,   "EMR":  0.230,   "MMM":  0.240,
    "HON":  0.220,   "GD":   0.210,   "LMT":  0.200,
    "UPS":  0.230,   "FDX":  0.270,   "UNP":  0.220,   "NSC":  0.220,
    "CSX":  0.230,   "PCAR": 0.250,   "ODFL": 0.260,   "FAST": 0.230,
    "CPRT": 0.260,   "JCI":  0.250,
    # ── Energy ────────────────────────────────────────────────────────────────
    "XOM":  0.270,   "CVX":  0.270,   "COP":  0.300,   "SLB":  0.330,
    "FANG": 0.380,
    # ── Utilities / telecom ───────────────────────────────────────────────────
    "NEE":  0.210,   "DUK":  0.185,   "SO":   0.180,   "XEL":  0.185,
    "EXC":  0.200,   "CEG":  0.280,   "T":    0.200,   "VZ":   0.195,
    "TMUS": 0.220,
    # ── Media / telecom ───────────────────────────────────────────────────────
    "CMCSA":0.235,   "CHTR": 0.290,   "DIS":  0.280,   "WBD":  0.430,
    # ── Other NDX / SP100 ─────────────────────────────────────────────────────
    "ACN":  0.235,   "IBM":  0.230,   "CSCO": 0.240,   "ADP":  0.210,
    "PAYX": 0.210,   "VRSK": 0.210,   "CSGP": 0.320,   "SPG":  0.270,
    "SIRI": 0.350,   "F":    0.360,   "GM":   0.330,   "DOW":  0.290,
    "ALGN": 0.370,   "HSIC": 0.230,   "D":    0.185,
    # BRK.B excluded — non-standard OCC symbol, Alpaca doesn't support it
}
_DEFAULT_IV_STOCK = 0.35   # lowered from 0.40; most large-caps trade below this


def _resolve_iv(ticker: str, item: dict, spot: float = 0.0) -> float:
    """
    Best available IV for a single options chain item.

    Search order:
      1. item["implied_volatility"]              — Alpaca impliedVolatility (best)
      2. item["greeks"]["implied_volatility"]    — some plan tiers embed it here
      3. item["session"]["implied_volatility"]   — session block
      4. item["day"]["implied_volatility"]       — day block
      5. compute_iv_from_quote(item, spot)       — Newton-Raphson from bid/ask mid
      6. GEX_DEFAULT_IV_<TICKER> env var         — operator override
      7. _DEFAULT_IV table                       — hardcoded estimate per ticker
      8. _DEFAULT_IV_STOCK catch-all
    """
    # 1–4: Polygon-provided IV fields
    # Cap at 3.0 (300%) — rejects garbage values Polygon occasionally returns
    # (e.g. 6.694 for illiquid deep-OTM strikes) while allowing extreme but
    # real volatility (TSLA ~0.80, MRNA ~0.60, binary biotech ~1.5+).
    IV_SANITY_MAX = 3.0
    for path in [
        lambda i: i.get("implied_volatility"),
        lambda i: (i.get("greeks") or {}).get("implied_volatility"),
        lambda i: (i.get("session") or {}).get("implied_volatility"),
        lambda i: (i.get("day") or {}).get("implied_volatility"),
    ]:
        try:
            v = float(path(item) or 0)
            if 0.001 < v <= IV_SANITY_MAX:
                return v
        except Exception:
            pass

    # 5: Newton-Raphson IV from bid/ask — always available on Options Starter
    if spot > 0:
        try:
            iv = compute_iv_from_quote(item, spot)
            if iv > 0.001:
                return iv
        except Exception:
            pass

    # 6: env override  e.g.  GEX_DEFAULT_IV_SPX=0.185
    env_val = os.getenv(f"GEX_DEFAULT_IV_{ticker.upper()}", "")
    if env_val:
        try:
            v = float(env_val)
            if v > 0.001:
                return v
        except Exception:
            pass

    # 7–8: built-in table + catch-all
    return _DEFAULT_IV.get(ticker.upper(), _DEFAULT_IV_STOCK)

class SnapshotStore:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path))
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                underlying  TEXT NOT NULL,
                ts          TEXT NOT NULL,
                spot        REAL NOT NULL,
                gex_json    TEXT NOT NULL)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_u_ts ON snapshots(underlying, ts)")

            con.execute("""CREATE TABLE IF NOT EXISTS levels_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                underlying  TEXT NOT NULL,
                ts          TEXT NOT NULL,
                spot        REAL NOT NULL,
                zero_gamma  REAL,
                gamma_wall  REAL,
                call_wall   REAL,
                put_wall    REAL,
                hv_strike   REAL,
                net_gex_b   REAL,
                regime      TEXT,
                strength    REAL,
                gex_skew    REAL,
                dist_flip   REAL,
                net_0_1     REAL,
                net_2_7     REAL,
                net_8_45    REAL)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_lh_u_ts ON levels_history(underlying, ts)")

            con.execute("""CREATE TABLE IF NOT EXISTS alerts_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id    TEXT UNIQUE,
                underlying  TEXT NOT NULL,
                ts          TEXT NOT NULL,
                alert_type  TEXT NOT NULL,
                severity    TEXT NOT NULL,
                msg         TEXT NOT NULL,
                spot        REAL NOT NULL,
                extra_json  TEXT)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ah_u_ts ON alerts_history(underlying, ts)")

            con.execute("""CREATE TABLE IF NOT EXISTS trade_ideas (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                idea_id       TEXT UNIQUE,
                underlying    TEXT NOT NULL,
                ts            TEXT NOT NULL,
                spot          REAL,
                regime        TEXT,
                strategy      TEXT,
                direction     TEXT,
                confidence    TEXT,
                trade_type    TEXT DEFAULT 'spread',
                legs_json     TEXT,
                setup         TEXT,
                entry_condition TEXT,
                gex_rationale TEXT,
                tags_json     TEXT,
                max_profit    TEXT,
                max_loss      TEXT,
                risk_reward   TEXT,
                target_credit TEXT,
                source        TEXT DEFAULT 'dashboard',
                cached        INTEGER DEFAULT 0)""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ti_u_ts ON trade_ideas(underlying, ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ti_ts    ON trade_ideas(ts)")

            con.execute("""CREATE TABLE IF NOT EXISTS pre_market_levels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                underlying  TEXT NOT NULL,
                date        TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                spot        REAL,
                -- full-chain levels
                zero_gamma  REAL, call_wall REAL, put_wall REAL,
                hv_strike   REAL, net_gex_b REAL, regime TEXT,
                strength    REAL, gex_skew  REAL, dist_flip REAL,
                -- 0-1 DTE bucket levels
                b01_zero_gamma REAL, b01_call_wall REAL, b01_put_wall  REAL,
                b01_net_gex_b  REAL, b01_regime    TEXT,
                UNIQUE(underlying, date))""")
            con.execute("CREATE INDEX IF NOT EXISTS idx_pm_u_d ON pre_market_levels(underlying, date)")
            con.commit()

    # ── Levels ────────────────────────────────────────────────────────────────

    def insert_levels(self, underlying: str, ts_iso: str, spot: float,
                      levels: dict, bucketed_net: dict) -> None:
        try:
            with sqlite3.connect(self.db_path) as con:
                con.execute("""INSERT INTO levels_history
                    (underlying,ts,spot,zero_gamma,gamma_wall,call_wall,put_wall,
                     hv_strike,net_gex_b,regime,strength,gex_skew,dist_flip,
                     net_0_1,net_2_7,net_8_45)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    underlying, ts_iso, float(spot),
                    levels.get("zero_gamma"), levels.get("gamma_wall"),
                    levels.get("call_wall"),  levels.get("put_wall"),
                    levels.get("hv_strike"),  levels.get("net_gex_b"),
                    levels.get("regime"),     levels.get("regime_strength"),
                    levels.get("gex_skew"),   levels.get("dist_to_flip_pct"),
                    bucketed_net.get("0_1", 0.0),
                    bucketed_net.get("2_7", 0.0),
                    bucketed_net.get("8_45", 0.0),
                ))
                con.commit()
        except Exception as e:
            log.warning("insert_levels: %s", e)

    def load_levels_history(self, underlying: str, days: int = 5) -> List[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as con:
                rows = con.execute("""
                    SELECT ts,spot,zero_gamma,gamma_wall,call_wall,put_wall,
                           net_gex_b,regime,strength,net_0_1,net_2_7,net_8_45
                    FROM levels_history WHERE underlying=? AND ts>=?
                    ORDER BY ts ASC""",
                    (underlying, cutoff)).fetchall()
            return [
                {"ts": r[0], "spot": r[1], "zero_gamma": r[2], "gamma_wall": r[3],
                 "call_wall": r[4], "put_wall": r[5], "net_gex_b": r[6],
                 "regime": r[7], "strength": r[8],
                 "net_0_1": r[9], "net_2_7": r[10], "net_8_45": r[11]}
                for r in rows
            ]
        except Exception as e:
            log.warning("load_levels_history: %s", e)
            return []

    # ── Alerts ────────────────────────────────────────────────────────────────

    def insert_alert(self, alert: dict) -> None:
        try:
            with sqlite3.connect(self.db_path) as con:
                extra = {k: v for k, v in alert.items()
                         if k not in ("id", "time", "ticker", "type", "severity", "msg", "spot")}
                con.execute("""INSERT OR IGNORE INTO alerts_history
                    (alert_id,underlying,ts,alert_type,severity,msg,spot,extra_json)
                    VALUES(?,?,?,?,?,?,?,?)""", (
                    alert.get("id", ""),   alert.get("ticker", ""),
                    alert.get("time", ""), alert.get("type", ""),
                    alert.get("severity", ""), alert.get("msg", ""),
                    float(alert.get("spot", 0)),
                    json.dumps(extra) if extra else None,
                ))
                con.commit()
        except Exception as e:
            log.warning("insert_alert: %s", e)

    def load_alerts_history(self, underlying: Optional[str] = None,
                            days: int = 7, limit: int = 200) -> List[dict]:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as con:
                if underlying:
                    rows = con.execute("""
                        SELECT alert_id,underlying,ts,alert_type,severity,msg,spot,extra_json
                        FROM alerts_history WHERE underlying=? AND ts>=?
                        ORDER BY ts DESC LIMIT ?""",
                        (underlying, cutoff, limit)).fetchall()
                else:
                    rows = con.execute("""
                        SELECT alert_id,underlying,ts,alert_type,severity,msg,spot,extra_json
                        FROM alerts_history WHERE ts>=?
                        ORDER BY ts DESC LIMIT ?""",
                        (cutoff, limit)).fetchall()
            return [
                {"id": r[0], "ticker": r[1], "time": r[2], "type": r[3],
                 "severity": r[4], "msg": r[5], "spot": r[6],
                 **(json.loads(r[7]) if r[7] else {})}
                for r in rows
            ]
        except Exception as e:
            log.warning("load_alerts_history: %s", e)
            return []

    # ── Snapshots ─────────────────────────────────────────────────────────────

    def insert_snapshot(self, underlying: str, ts_iso: str, spot: float, gex: dict):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO snapshots(underlying,ts,spot,gex_json) VALUES(?,?,?,?)",
                (underlying, ts_iso, float(spot),
                 json.dumps({str(k): v for k, v in gex.items()}, separators=(",", ":"))))
            con.commit()

    def load_recent(self, underlying: str, limit: int = 780) -> List[dict]:
        with sqlite3.connect(self.db_path) as con:
            rows = con.execute(
                "SELECT ts,spot,gex_json FROM snapshots "
                "WHERE underlying=? ORDER BY ts DESC LIMIT ?",
                (underlying, int(limit))).fetchall()
        out = []
        for ts, spot, gex_json in reversed(rows):
            raw = json.loads(gex_json)
            gex = {float(k): float(v) for k, v in raw.items()}
            out.append({"timestamp": pd.to_datetime(ts), "spot": float(spot), "gex": gex})
        return out

    def prune_old(self, underlying: str, keep_days: int = 5):
        cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
        with sqlite3.connect(self.db_path) as con:
            con.execute("DELETE FROM snapshots WHERE underlying=? AND ts<?", (underlying, cutoff))
            con.commit()

    # ── Trade Ideas ───────────────────────────────────────────────────────────

    def insert_trade_ideas(self, underlying: str, ideas: list, spot: float,
                           regime: str, source: str = "dashboard",
                           cached: bool = False) -> int:
        """
        Persist a batch of trade ideas for one ticker.
        Each idea gets a deterministic idea_id = underlying_strategy_ts_hash
        so re-inserting the same cached result is idempotent (INSERT OR IGNORE).
        Returns number of new rows inserted.
        """
        if not ideas:
            return 0
        ts  = datetime.utcnow().isoformat()
        inserted = 0
        try:
            with sqlite3.connect(self.db_path) as con:
                for idea in ideas:
                    import hashlib
                    raw = f"{underlying}_{idea.get('strategy','')}_{ts}_{idea.get('direction','')}"
                    idea_id = hashlib.md5(raw.encode()).hexdigest()[:16]
                    con.execute("""INSERT OR IGNORE INTO trade_ideas
                        (idea_id,underlying,ts,spot,regime,strategy,direction,confidence,
                         trade_type,legs_json,setup,entry_condition,gex_rationale,
                         tags_json,max_profit,max_loss,risk_reward,target_credit,
                         source,cached)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        idea_id, underlying, ts, float(spot), regime,
                        idea.get("strategy",""),      idea.get("direction",""),
                        idea.get("confidence",""),    idea.get("trade_type","spread"),
                        json.dumps(idea.get("legs") or []),
                        idea.get("setup",""),         idea.get("entry_condition",""),
                        idea.get("gex_rationale",""),
                        json.dumps(idea.get("tags") or []),
                        idea.get("max_profit",""),    idea.get("max_loss",""),
                        idea.get("risk_reward",""),   idea.get("target_credit_debit",""),
                        source, int(cached),
                    ))
                    inserted += con.execute(
                        "SELECT changes()").fetchone()[0]
                con.commit()
        except Exception as e:
            log.warning("insert_trade_ideas: %s", e)
        return inserted

    def load_trade_ideas_history(self, underlying: Optional[str] = None,
                                 days: int = 7, limit: int = 200,
                                 source: Optional[str] = None) -> List[dict]:
        """Load persisted trade ideas, optionally filtered by ticker/days/source."""
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        try:
            with sqlite3.connect(self.db_path) as con:
                filters = ["ts >= ?"]
                params: list = [cutoff]
                if underlying:
                    filters.append("underlying = ?"); params.append(underlying.upper())
                if source:
                    filters.append("source = ?"); params.append(source)
                where = " AND ".join(filters)
                rows = con.execute(f"""
                    SELECT idea_id,underlying,ts,spot,regime,strategy,direction,
                           confidence,trade_type,legs_json,setup,entry_condition,
                           gex_rationale,tags_json,max_profit,max_loss,
                           risk_reward,target_credit,source,cached
                    FROM trade_ideas WHERE {where}
                    ORDER BY ts DESC LIMIT ?""",
                    params + [limit]).fetchall()
            result = []
            for r in rows:
                result.append({
                    "idea_id": r[0], "ticker": r[1], "ts": r[2],
                    "spot": r[3], "regime": r[4], "strategy": r[5],
                    "direction": r[6], "confidence": r[7], "trade_type": r[8],
                    "legs": json.loads(r[9] or "[]"),
                    "setup": r[10], "entry_condition": r[11],
                    "gex_rationale": r[12],
                    "tags": json.loads(r[13] or "[]"),
                    "max_profit": r[14], "max_loss": r[15],
                    "risk_reward": r[16], "target_credit_debit": r[17],
                    "source": r[18], "cached": bool(r[19]),
                })
            return result
        except Exception as e:
            log.warning("load_trade_ideas_history: %s", e)
            return []


    # ── Pre-market level snapshots ────────────────────────────────────────────

    def insert_pre_market_levels(
        self,
        underlying:     str,
        date_iso:       str,          # "YYYY-MM-DD"
        captured_at:    str,          # ISO datetime
        spot:           float,
        levels:         dict,         # full-chain compute_key_levels() output
        levels_0_1:     dict,         # 0-1 DTE bucket compute_key_levels() output
    ) -> bool:
        """
        Persist pre-market GEX levels for today.
        UNIQUE(underlying, date) — safe to call multiple times; first write wins.
        Returns True if a new row was inserted, False if already existed.
        """
        try:
            with sqlite3.connect(self.db_path) as con:
                con.execute("""
                    INSERT OR IGNORE INTO pre_market_levels
                    (underlying, date, captured_at, spot,
                     zero_gamma, call_wall, put_wall, hv_strike,
                     net_gex_b, regime, strength, gex_skew, dist_flip,
                     b01_zero_gamma, b01_call_wall, b01_put_wall,
                     b01_net_gex_b, b01_regime)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    underlying.upper(), date_iso, captured_at, float(spot),
                    levels.get("zero_gamma"),  levels.get("call_wall"),
                    levels.get("put_wall"),    levels.get("hv_strike"),
                    levels.get("net_gex_b"),   levels.get("regime"),
                    levels.get("regime_strength"), levels.get("gex_skew"),
                    levels.get("dist_to_flip_pct"),
                    levels_0_1.get("zero_gamma"),  levels_0_1.get("call_wall"),
                    levels_0_1.get("put_wall"),    levels_0_1.get("net_gex_b"),
                    levels_0_1.get("regime"),
                ))
                new_row = con.execute("SELECT changes()").fetchone()[0] > 0
                con.commit()
            return new_row
        except Exception as e:
            log.warning("insert_pre_market_levels: %s", e)
            return False

    def load_pre_market_levels(
        self, underlying: str, date_iso: Optional[str] = None
    ) -> Optional[dict]:
        """
        Load pre-market snapshot for a ticker.
        If date_iso is None, returns the most recent snapshot (today or yesterday).
        Returns None if no snapshot exists.
        """
        try:
            with sqlite3.connect(self.db_path) as con:
                if date_iso:
                    row = con.execute("""
                        SELECT underlying, date, captured_at, spot,
                               zero_gamma, call_wall, put_wall, hv_strike,
                               net_gex_b, regime, strength, gex_skew, dist_flip,
                               b01_zero_gamma, b01_call_wall, b01_put_wall,
                               b01_net_gex_b, b01_regime
                        FROM pre_market_levels WHERE underlying=? AND date=?""",
                        (underlying.upper(), date_iso)).fetchone()
                else:
                    row = con.execute("""
                        SELECT underlying, date, captured_at, spot,
                               zero_gamma, call_wall, put_wall, hv_strike,
                               net_gex_b, regime, strength, gex_skew, dist_flip,
                               b01_zero_gamma, b01_call_wall, b01_put_wall,
                               b01_net_gex_b, b01_regime
                        FROM pre_market_levels WHERE underlying=?
                        ORDER BY date DESC LIMIT 1""",
                        (underlying.upper(),)).fetchone()
            if not row:
                return None
            return {
                "underlying":   row[0],  "date":        row[1],
                "captured_at":  row[2],  "spot":        row[3],
                "levels": {
                    "zero_gamma":      row[4],  "call_wall":  row[5],
                    "put_wall":        row[6],  "hv_strike":  row[7],
                    "net_gex_b":       row[8],  "regime":     row[9],
                    "regime_strength": row[10], "gex_skew":   row[11],
                    "dist_to_flip_pct": row[12],
                },
                "levels_0_1": {
                    "zero_gamma": row[13], "call_wall":  row[14],
                    "put_wall":   row[15], "net_gex_b":  row[16],
                    "regime":     row[17],
                },
            }
        except Exception as e:
            log.warning("load_pre_market_levels: %s", e)
            return None


# ---------------------------------------------------------------------------
# GEX Engine
# ---------------------------------------------------------------------------

class GEXEngine:
    def __init__(self, store: Optional[SnapshotStore] = None, underlying: str = ""):
        self.contract_data: Dict[str, Dict[str, Any]] = {}
        self.spot_price:    float                     = 0.0
        self.history:       List[dict]                = []
        self.store          = store
        self.underlying     = underlying
        self.ws_spot_ts:    float                     = 0.0   # epoch of last WS trade update
        self.spot_source:   str                       = ""    # "ws" | "rest" | "seed" | ""

    def update_spot(self, price: float, source: str = "") -> None:
        """
        Update spot price.  source="ws" marks this as a live WS trade —
        REST pollers will not overwrite a WS price until it is stale.
        """
        if price and price > 0:
            self.spot_price = float(price)
            if source:
                self.spot_source = source
            if source == "ws":
                self.ws_spot_ts = time.time()

    def update_contract(self, ticker: str, strike: float, ctype: str,
                        gamma: float, oi: int, expiry=None,
                        volume: int = 0, iv: float = 0.0):
        self.contract_data[ticker] = {
            "strike": float(strike),
            "type":   str(ctype).lower(),
            "gamma":  float(gamma or 0.0),
            "oi":     int(oi or 0),
            "expiry": str(expiry or ""),
            "volume": int(volume or 0),
            "iv":     float(iv or 0.0),
        }

    def compute_gex_by_strike(self) -> Dict[float, float]:
        if self.spot_price <= 0:
            return {}
        S       = self.spot_price
        gex_map = defaultdict(float)
        for c in self.contract_data.values():
            raw = c["gamma"] * CONTRACT_SIZE * c["oi"] * (S ** 2) * 0.01
            gex_map[c["strike"]] += raw if c["type"] == "call" else -raw
        return dict(gex_map)

    def compute_gex_by_expiry_strike(self) -> Dict[str, Dict[float, float]]:
        """Returns {expiry_iso: {strike: net_gex}} — strike × expiry matrix."""
        if self.spot_price <= 0:
            return {}
        S   = self.spot_price
        out: Dict[str, Any] = defaultdict(lambda: defaultdict(float))
        for c in self.contract_data.values():
            exp = c.get("expiry", "")
            if not exp:
                continue
            raw = c["gamma"] * CONTRACT_SIZE * c["oi"] * (S ** 2) * 0.01
            out[exp][c["strike"]] += raw if c["type"] == "call" else -raw
        return {k: dict(v) for k, v in sorted(out.items())}

    @staticmethod
    def _dte_bucket(expiry_str: str) -> str:
        """
        Classify expiry into DTE bucket.
          '0_1'  — 0-1 DTE  : intraday/overnight gamma, pin risk
          '2_7'  — 2-7 DTE  : weekly walls, dominant near-term positioning
          '8_45' — 8-45 DTE : structural / monthly walls
          'far'  — >45 DTE  : LEAPS, low gamma per dollar
        """
        try:
            exp = datetime.strptime(str(expiry_str), "%Y-%m-%d").date()
            dte = (exp - date.today()).days
        except Exception:
            return "unknown"
        if dte <= 1:  return "0_1"
        if dte <= 7:  return "2_7"
        if dte <= 45: return "8_45"
        return "far"

    def compute_gex_bucketed(self) -> Dict[str, Dict[float, float]]:
        """
        GEX by strike broken into DTE buckets.
        Returns {bucket: {strike: net_gex_dollars}}.

        Why this matters:
          0_1  — expires today; don't rely on it tomorrow
          2_7  — weekly walls; swing trading horizon
          8_45 — structural; holds for weeks; flip = real regime change
        """
        if self.spot_price <= 0:
            return {}
        S   = self.spot_price
        out = {"0_1": defaultdict(float), "2_7": defaultdict(float),
               "8_45": defaultdict(float), "far": defaultdict(float)}
        for c in self.contract_data.values():
            bucket = self._dte_bucket(c.get("expiry", ""))
            if bucket not in out:
                continue
            raw = c["gamma"] * CONTRACT_SIZE * c["oi"] * (S ** 2) * 0.01
            out[bucket][c["strike"]] += raw if c["type"] == "call" else -raw
        return {k: dict(v) for k, v in out.items() if v}

    def snapshot(self):
        gex = self.compute_gex_by_strike()
        if not gex:
            return
        ts = datetime.utcnow()
        self.history.append({"timestamp": ts, "gex": gex, "spot": self.spot_price})
        if len(self.history) > 780:
            self.history = self.history[-780:]
        if self.store and self.underlying:
            try:
                self.store.insert_snapshot(
                    self.underlying, ts.isoformat(), self.spot_price, gex)
            except Exception as e:
                log.warning("DB snapshot: %s", e)

    def restore_from_store(self, limit: int = 780) -> int:
        if not self.store or not self.underlying:
            return 0
        self.history = self.store.load_recent(self.underlying, limit=limit)
        if self.history:
            self.spot_price = float(self.history[-1]["spot"])
        return len(self.history)

    def get_matrix(self, hours_back: Optional[float] = None
                   ) -> Tuple[list, list, np.ndarray, list]:
        if not self.history:
            return [], [], np.array([]), []
        df = pd.DataFrame(self.history)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if hours_back is not None:
            cutoff = df["timestamp"].max() - timedelta(hours=hours_back)
            df = df[df["timestamp"] >= cutoff]
        if df.empty:
            return [], [], np.array([]), []
        all_strikes = sorted({s for g in df["gex"] for s in g})
        times, spots = df["timestamp"].tolist(), df["spot"].tolist()
        matrix = np.zeros((len(all_strikes), len(times)))
        sidx   = {s: i for i, s in enumerate(all_strikes)}
        for t_idx, row in enumerate(df["gex"]):
            for strike, val in row.items():
                matrix[sidx[strike], t_idx] = val
        return times, all_strikes, matrix / 1e9, spots

    def compute_iv_rank(self) -> Dict[str, Any]:
        """
        Compute IV Rank and IV Percentile from current contract IVs.

        IV Rank   = (current_IV - 52w_low) / (52w_high - 52w_low) × 100
        IV Pct    = percentile of current IV among all stored IV samples

        Since we don't have 52-week history from Polygon Starter, we approximate
        using the cross-sectional distribution of IVs across all loaded contracts:
          - current_iv  = OI-weighted average IV of ATM contracts (±5% of spot)
          - iv_low/high = 5th/95th pct of all non-zero IVs in the chain
          - iv_pct      = where current_iv sits in the full chain distribution

        This gives a meaningful same-day IV rank that reflects skew and term
        structure even without historical data.

        Returns dict with: current_iv, iv_rank, iv_percentile, atm_iv,
                           call_iv_avg, put_iv_avg, skew (call - put ATM IV),
                           contracts_with_iv (coverage %)
        """
        ivs     = [c["iv"] for c in self.contract_data.values() if c["iv"] > 0.001]
        if len(ivs) < 5 or self.spot_price <= 0:
            return {}

        ivs_arr = sorted(ivs)
        n       = len(ivs_arr)

        # ATM contracts (±5% of spot)
        S = self.spot_price
        atm = [c for c in self.contract_data.values()
               if c["iv"] > 0.001 and abs(c["strike"] - S) / S <= 0.05]
        atm_calls = [c["iv"] for c in atm if c["type"] == "call"]
        atm_puts  = [c["iv"] for c in atm if c["type"] == "put"]

        atm_iv     = float(np.mean([c["iv"] for c in atm])) if atm else float(np.median(ivs_arr))
        call_iv    = float(np.mean(atm_calls)) if atm_calls else 0.0
        put_iv     = float(np.mean(atm_puts))  if atm_puts  else 0.0

        # IV percentile: where does current ATM IV sit in full chain distribution?
        iv_pct = sum(1 for v in ivs_arr if v <= atm_iv) / n * 100

        # IV rank: position between 5th and 95th percentile (robust to outliers)
        iv_lo  = ivs_arr[max(0, int(n * 0.05))]
        iv_hi  = ivs_arr[min(n - 1, int(n * 0.95))]
        iv_rank = ((atm_iv - iv_lo) / (iv_hi - iv_lo) * 100
                   if iv_hi > iv_lo else 50.0)
        iv_rank = max(0.0, min(100.0, iv_rank))

        total_contracts = len(self.contract_data)
        coverage_pct    = round(len(ivs) / total_contracts * 100, 1) if total_contracts else 0

        return {
            "current_iv":       round(atm_iv, 4),
            "atm_iv":           round(atm_iv, 4),
            "call_iv_avg":      round(call_iv, 4),
            "put_iv_avg":       round(put_iv, 4),
            "iv_skew":          round(call_iv - put_iv, 4),   # +ve = call vol > put vol
            "iv_rank":          round(iv_rank, 1),
            "iv_percentile":    round(iv_pct, 1),
            "iv_low":           round(iv_lo, 4),
            "iv_high":          round(iv_hi, 4),
            "contracts_with_iv": coverage_pct,
            "iv_count":         len(ivs),
        }

    def compute_unusual_volume(
        self,
        vol_oi_threshold: float = 0.50,   # vol/OI > 50% = unusual
        min_volume:       int   = 100,     # ignore tiny prints
        min_oi:           int   = 50,      # ignore illiquid
        top_n:            int   = 20,      # max contracts to return
    ) -> List[dict]:
        """
        Detect contracts with unusual options volume relative to open interest.

        A contract is flagged when:
          volume / open_interest > vol_oi_threshold  (default 50%)
          volume >= min_volume
          open_interest >= min_oi

        High vol/OI means new positions are being opened aggressively today —
        informed flow (earnings plays, directional bets, hedges being placed).

        Returns top_n contracts sorted by vol/OI ratio descending.
        Each entry: ticker, strike, type, expiry, volume, oi, vol_oi_ratio,
                    iv, dte, moneyness_pct
        """
        if self.spot_price <= 0:
            return []

        S       = self.spot_price
        today   = date.today()
        unusual = []

        for sym, c in self.contract_data.items():
            vol = c.get("volume", 0)
            oi  = c.get("oi", 0)
            if vol < min_volume or oi < min_oi:
                continue
            ratio = vol / oi
            if ratio < vol_oi_threshold:
                continue
            try:
                exp     = datetime.strptime(c["expiry"], "%Y-%m-%d").date() if c["expiry"] else None
                dte     = (exp - today).days if exp else 0
            except Exception:
                dte = 0
            moneyness = round((c["strike"] - S) / S * 100, 2)   # +ve = OTM call, -ve = OTM put
            unusual.append({
                "symbol":       sym,
                "strike":       c["strike"],
                "type":         c["type"],
                "expiry":       c["expiry"],
                "dte":          dte,
                "volume":       vol,
                "oi":           oi,
                "vol_oi_ratio": round(ratio, 3),
                "iv":           round(c.get("iv", 0), 4),
                "moneyness_pct": moneyness,
                "direction":    "call" if c["type"] == "call" else "put",
                "sentiment":    ("bullish" if c["type"] == "call" and moneyness > 0
                                 else "bearish" if c["type"] == "put" and moneyness < 0
                                 else "neutral"),
            })

        unusual.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
        return unusual[:top_n]


# ---------------------------------------------------------------------------
# Quant key levels
# ---------------------------------------------------------------------------

def compute_key_levels(gex_map: Dict[float, float], spot: float) -> Dict[str, Any]:
    """
    Full quant-grade level set from a GEX-by-strike dict.

    zero_gamma       — interpolated strike where cumulative GEX = 0 (regime divider)
    gamma_wall       — strike with highest |GEX| (price magnet)
    call_wall        — strike with highest positive GEX (overhead resistance)
    put_wall         — strike with most negative GEX (downside support/accelerator)
    hv_strike        — highest |GEX| within ±5% of spot (pin target near expiry)
    net_gex_b        — total net GEX $B
    regime           — 'positive' (vol suppression) | 'negative' (vol amplification)
    regime_strength  — net / Σ|GEX|, range −1…+1
    gex_skew         — call_gex / |put_gex|  (>1 = call-heavy)
    dist_to_flip_pct — % distance from spot to zero_gamma
    """
    if not gex_map or spot <= 0:
        return {}

    strikes = sorted(gex_map.keys())
    vals    = [gex_map[s] for s in strikes]
    cum     = list(np.cumsum(vals))
    abs_v   = [abs(v) for v in vals]

    zero_gamma = None
    for i in range(1, len(cum)):
        if cum[i - 1] * cum[i] <= 0:
            s0, s1, c0, c1 = strikes[i-1], strikes[i], cum[i-1], cum[i]
            zg = (round(s0 + (s1 - s0) * (-c0 / (c1 - c0)), 2)
                  if c1 != c0 else round((s0 + s1) / 2, 2))
            # Reject crossings that are implausibly far from spot (>50%).
            # These are caused by deep OTM garbage strikes with tiny GEX
            # flipping the cumulative sum far outside the relevant range.
            if abs(zg - spot) / spot <= 0.50:
                zero_gamma = zg
                break

    gamma_wall = strikes[int(np.argmax(abs_v))]
    pos_pairs  = [(s, v) for s, v in zip(strikes, vals) if v > 0]
    neg_pairs  = [(s, v) for s, v in zip(strikes, vals) if v < 0]
    call_wall  = max(pos_pairs, key=lambda x: x[1])[0] if pos_pairs else None
    put_wall   = min(neg_pairs, key=lambda x: x[1])[0] if neg_pairs else None

    near      = [(s, abs(v)) for s, v in zip(strikes, vals) if abs(s - spot) / spot <= 0.05]
    hv_strike = max(near, key=lambda x: x[1])[0] if near else gamma_wall

    net_gex   = sum(vals)
    total_abs = sum(abs_v) or 1.0
    regime    = "positive" if net_gex >= 0 else "negative"
    strength  = round(net_gex / total_abs, 4)
    call_gex  = sum(v for v in vals if v > 0)
    put_gex   = abs(sum(v for v in vals if v < 0)) or 1.0
    skew      = round(call_gex / put_gex, 3)
    dist_flip = round((spot - zero_gamma) / spot * 100, 3) if zero_gamma else None

    return {
        "zero_gamma":       zero_gamma,
        "gamma_wall":       round(gamma_wall, 2),
        "call_wall":        round(call_wall, 2)  if call_wall  else None,
        "put_wall":         round(put_wall, 2)   if put_wall   else None,
        "hv_strike":        round(hv_strike, 2),
        "net_gex_b":        round(net_gex / 1e9, 4),
        "regime":           regime,
        "regime_strength":  strength,
        "gex_skew":         skew,
        "dist_to_flip_pct": dist_flip,
    }


# ---------------------------------------------------------------------------
# Gamma Slope  —  dGEX/dStrike
# ---------------------------------------------------------------------------

def compute_gamma_slope(
    gex_map: Dict[float, float],
    spot: float,
    near_pct: float = 0.08,
) -> Dict[str, Any]:
    """
    First derivative of the GEX profile: dGEX/dStrike per $1 strike move.

    High absolute slope = zone of large dealer hedging flow on a small price move.
    Uses np.gradient() which handles uneven strike spacing correctly.

    Returns
    -------
    strikes, slope_b_per_pt, slope_norm  — full arrays for charting
    at_spot    — slope interpolated at current spot + regime label
    inflection_strikes — where slope changes sign (GEX local extrema / pin zones)
    steepest_pos / steepest_neg — global fastest buildup / drain strikes
    near_spot  — stats in ±near_pct band: mean slope, momentum label
    """
    if not gex_map or spot <= 0 or len(gex_map) < 3:
        return {}

    strikes   = sorted(gex_map.keys())
    vals      = np.array([gex_map[s] for s in strikes], dtype=float)
    sarr      = np.array(strikes, dtype=float)
    slope_raw = np.gradient(vals, sarr)

    max_abs   = float(np.max(np.abs(slope_raw))) or 1.0
    slope_n   = slope_raw / max_abs

    s_at_spot  = float(np.interp(spot, sarr, slope_raw))
    sn_at_spot = float(np.interp(spot, sarr, slope_n))
    FLAT       = 0.10
    regime_at  = "flat" if abs(sn_at_spot) < FLAT else ("building" if s_at_spot > 0 else "collapsing")

    inflection: List[float] = []
    for i in range(1, len(slope_raw)):
        if slope_raw[i - 1] * slope_raw[i] < 0:
            s0, s1 = float(sarr[i-1]), float(sarr[i])
            d0, d1 = float(slope_raw[i-1]), float(slope_raw[i])
            cross  = s0 + (s1-s0)*(-d0/(d1-d0)) if d1 != d0 else (s0+s1)/2
            inflection.append(round(cross, 2))

    lo, hi    = spot*(1-near_pct), spot*(1+near_pct)
    near_mask = (sarr >= lo) & (sarr <= hi)
    ns        = slope_raw[near_mask]

    if len(ns) >= 2:
        mean_s  = float(np.mean(ns))
        pos_m   = near_mask & (slope_raw > 0)
        neg_m   = near_mask & (slope_raw < 0)
        mxp     = float(sarr[pos_m][int(np.argmax(slope_raw[pos_m]))]) if pos_m.any() else None
        mxn     = float(sarr[neg_m][int(np.argmin(slope_raw[neg_m]))]) if neg_m.any() else None
        net_sgn = 1.0 if float(np.sum(vals)) >= 0 else -1.0
        sl_sgn  = 1.0 if mean_s > 0 else -1.0
        if abs(mean_s / (max_abs or 1)) < FLAT:
            momentum = "neutral"
        elif sl_sgn == net_sgn:
            momentum = "accelerating"
        else:
            momentum = "decelerating"
    else:
        mean_s = mxp = mxn = None
        momentum = "neutral"

    ip = int(np.argmax(slope_raw));  in_ = int(np.argmin(slope_raw))

    return {
        "strikes":          [round(float(s), 2) for s in sarr],
        "slope_b_per_pt":   [round(float(v)/1e9, 6) for v in slope_raw],
        "slope_norm":       [round(float(v), 4) for v in slope_n],
        "at_spot": {
            "slope_b_per_pt": round(s_at_spot/1e9, 6),
            "slope_norm":     round(sn_at_spot, 4),
            "regime":         regime_at,
        },
        "inflection_strikes": inflection,
        "steepest_pos": {
            "strike":         round(float(sarr[ip]), 2),
            "slope_b_per_pt": round(float(slope_raw[ip])/1e9, 6),
            "slope_norm":     round(float(slope_n[ip]), 4),
        },
        "steepest_neg": {
            "strike":         round(float(sarr[in_]), 2),
            "slope_b_per_pt": round(float(slope_raw[in_])/1e9, 6),
            "slope_norm":     round(float(slope_n[in_]), 4),
        },
        "near_spot": {
            "pct_band":       near_pct,
            "mean_slope":     round(mean_s/1e9, 6) if mean_s is not None else None,
            "max_pos_strike": round(mxp, 2) if mxp is not None else None,
            "max_neg_strike": round(mxn, 2) if mxn is not None else None,
            "slope_momentum": momentum,
        },
    }


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def fast_bootstrap(engine: GEXEngine, underlying_ticker: str,
                   min_dte: int = 0, max_dte: int = 45) -> None:
    """
    Populate engine from Alpaca /v1beta1/options/chain in a single paginated pass.

    Gamma resolution order (per contract):
      1. greeks.gamma from Alpaca       — provided for all non-0DTE contracts
      2. BS gamma from Alpaca IV        — gamma missing but impliedVolatility present
      3. BS gamma from NR (bid/ask)     — IV missing, compute from mid price
      4. BS gamma from _DEFAULT_IV      — 0DTE or no quote data available

    Spot: seeded via get_spot() before the loop (Alpaca latest trade).
    No underlying_asset.price field in Alpaca chain — spot is REST-only.
    """
    log.info("[%s] Fast bootstrap DTE %d-%d ...", underlying_ticker, min_dte, max_dte)
    today = date.today()

    # Seed spot before loop — Alpaca latest trade is reliable for all equities/ETFs
    seed = get_spot(underlying_ticker)
    if seed > 0:
        engine.update_spot(seed)

    # Fetch OI from trading API — snapshots endpoint doesn't include OI.
    # fetch_oi_map() returns {occ_symbol: oi}; passed into iter_options_chain
    # so each contract gets real OI without a second paginated loop.
    oi_map = fetch_oi_map(underlying_ticker, min_dte=min_dte, max_dte=max_dte)
    if not oi_map:
        log.warning("[%s] OI map empty — GEX walls will be gamma-only (no OI weighting)",
                    underlying_ticker)

    updated = skipped = 0
    n_alpaca = n_bs_iv = n_bs_dv = 0
    _diag_logged = False

    for item in iter_options_chain(underlying_ticker, min_dte=min_dte, max_dte=max_dte,
                                   oi_map=oi_map):
        det     = item.get("details") or {}
        oticker = det.get("ticker")
        if not oticker:
            continue

        try:
            exp_date = datetime.strptime(det.get("expiration_date", ""), "%Y-%m-%d").date()
        except Exception:
            continue

        ctype  = str(det.get("contract_type", "")).lower()
        strike = float(det.get("strike_price") or 0)
        if ctype not in ("call", "put") or strike <= 0:
            continue

        greeks = item.get("greeks") or {}
        oi     = int(item.get("open_interest") or 0)
        gamma  = float(greeks.get("gamma") or 0)

        # Read fresh spot each iteration — get_spot() ran once before the loop
        # so this is stable; no UA price updates mid-loop with Alpaca.
        S = engine.spot_price

        # ── IV resolution — single source of truth for all branches ───────────
        iv = _resolve_iv(underlying_ticker, item, spot=S) if S > 0 else 0.0

        # ── Gamma resolution ──────────────────────────────────────────────────
        if gamma != 0.0:
            n_alpaca += 1
        elif S > 0 and iv > 0:
            T     = max((exp_date - today).days, 0) / 365.0
            gamma = _bs_gamma(S, strike, T, iv)

            if not _diag_logged and gamma != 0.0:
                _diag_logged = True
                mid_src = item.get("_iv_mid_src", "")
                if float(item.get("implied_volatility") or 0) > 0:
                    iv_src = "Alpaca IV"
                elif mid_src:
                    iv_src = f"NR/{mid_src} ({iv:.3f})"
                else:
                    iv_src = f"default table ({iv:.3f})"
                log.info(
                    "[%s] gamma=0 from Alpaca — BS fallback: "
                    "spot=%.2f K=%.0f T=%.4f iv=%.4f (%s) → gamma=%.7f",
                    underlying_ticker, S, strike, T, iv, iv_src, gamma,
                )

            if gamma != 0.0:
                mid_src = item.get("_iv_mid_src", "")
                if float(item.get("implied_volatility") or 0) > 0 or mid_src:
                    n_bs_iv += 1   # Alpaca IV or NR from bid/ask
                else:
                    n_bs_dv += 1   # default table (0DTE or no quote)

        engine.update_contract(
            oticker, strike, ctype, gamma, oi,
            expiry=exp_date,
            volume=int((item.get("day") or {}).get("volume") or 0),
            iv=iv,
        )
        updated += 1

    if updated == 0:
        log.warning("[%s] Fast bootstrap got 0 contracts", underlying_ticker)
        return

    # Post-loop spot recovery: if get_spot() returned 0 before the loop
    # (e.g. market closed, Alpaca rate limit), retry once now.
    if engine.spot_price <= 0:
        p = get_spot(underlying_ticker)
        if p > 0:
            engine.update_spot(p)
            log.info("[%s] spot recovered post-loop: %.2f", underlying_ticker, p)
            # Recompute BS gamma for contracts stored with S=0 during the loop
            S          = engine.spot_price
            recomputed = 0
            for sym, c in engine.contract_data.items():
                if c["gamma"] == 0.0 and c["iv"] == 0.0:
                    try:
                        exp = datetime.strptime(c["expiry"], "%Y-%m-%d").date()
                        T   = max((exp - today).days, 0) / 365.0
                        iv  = _DEFAULT_IV.get(underlying_ticker.upper(), _DEFAULT_IV_STOCK)
                        g   = _bs_gamma(S, c["strike"], T, iv)
                        if g != 0.0:
                            engine.contract_data[sym]["gamma"] = g
                            engine.contract_data[sym]["iv"]    = iv
                            recomputed += 1
                    except Exception:
                        pass
            if recomputed:
                log.info("[%s] recomputed BS gamma for %d contracts after spot recovery",
                         underlying_ticker, recomputed)
        else:
            log.warning("[%s] spot=0 after full bootstrap — Alpaca returned no price",
                        underlying_ticker)

    engine.snapshot()
    net     = sum(engine.compute_gex_by_strike().values())
    nonzero = sum(1 for c in engine.contract_data.values() if c["gamma"] != 0.0)

    log.info(
        "[%s] Bootstrap done — %d contracts, %d skipped, spot=%.2f, GEX=%.3fB | "
        "gamma: alpaca=%d bs_nr=%d bs_table=%d | non-zero=%d/%d",
        underlying_ticker, updated, skipped, engine.spot_price, net / 1e9,
        n_alpaca, n_bs_iv, n_bs_dv, nonzero, updated,
    )

    if nonzero == 0:
        log.error(
            "[%s] GEX=0 — all gammas are 0. "
            "Likely cause: spot=0 (BS skipped) or all contracts are 0DTE. "
            "Check ALPACA_API_KEY and that get_spot() returns a valid price.",
            underlying_ticker,
        )



