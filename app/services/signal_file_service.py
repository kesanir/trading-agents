"""
Signal File Service

Writes the JSON signal file that NinjaTrader polls on each bar close.
File path: {signal_dir}/{INSTRUMENT}_signal.json

The payload's `date` field is set to *today* (the current trading session).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_SIGNAL_DIR = os.environ.get("NT_SIGNAL_DIR", "signals")
DEFAULT_EXPIRY_MINS = int(os.environ.get("NT_SIGNAL_EXPIRY_MINS", "30"))


def write_signal_file(
    instrument: str,
    signal_data: Dict[str, Any],
    contracts: int,
    signal_dir: str = DEFAULT_SIGNAL_DIR,
    expiry_mins: int = DEFAULT_EXPIRY_MINS,
) -> str:
    """
    Write JSON signal file for NinjaTrader to consume.

    File format: {signal_dir}/{INSTRUMENT}_signal.json
    NinjaScript reads this file on each bar close.
    """
    os.makedirs(signal_dir, exist_ok=True)

    today = date.today()

    payload = {
        "instrument":  instrument,
        "timestamp":   datetime.utcnow().isoformat() + "Z",
        "date":        today.isoformat(),
        "signal":      float(signal_data["signal"]),
        "position":    signal_data["position"],          # LONG | SHORT | FLAT
        "contracts":   int(contracts),
        "confidence":  round(float(signal_data["confidence"]), 4),
        "vol_scale":   round(float(signal_data["vol_scale"]), 4),
        "current_vol": round(float(signal_data["current_vol"]), 6),
        "features":    {
            k: round(float(v), 4)
            for k, v in (signal_data.get("features") or {}).items()
            if v is not None
        },
        "status":      "PENDING",                        # NinjaTrader sets to EXECUTED
        "expiry_mins": int(expiry_mins),
    }

    path = os.path.join(signal_dir, f"{instrument}_signal.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    logger.info(
        "[%s] Signal written -> %s %d contract(s) | signal=%.3f | confidence=%.2f%%",
        instrument,
        payload["position"],
        payload["contracts"],
        payload["signal"],
        payload["confidence"] * 100,
    )
    return path


def _bias_to_position(bias: Any) -> str:
    b = str(bias or "").upper()
    if b in {"LONG", "BULLISH", "BUY"}:
        return "LONG"
    if b in {"SHORT", "BEARISH", "SELL"}:
        return "SHORT"
    return "FLAT"


def _position_sign(position: str) -> int:
    return {"LONG": 1, "SHORT": -1, "FLAT": 0}.get(position, 0)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_signal_data(
    trigger_obj: Dict[str, Any],
    intraday_apmem: Any,
    market: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Translate the internal trigger + intraday APMEM state into the generic
    `signal_data` dict expected by `write_signal_file`.

    `intraday_apmem` may be either an IntradayApmemSignal dataclass or its
    .to_dict() form — both shapes are handled.
    """
    market = market or {}

    def _ap(key: str, default: float = 0.0) -> float:
        if isinstance(intraday_apmem, dict):
            return _f(intraday_apmem.get(key), default)
        return _f(getattr(intraday_apmem, key, default), default)

    position = _bias_to_position(trigger_obj.get("bias"))
    confidence = _f(trigger_obj.get("confidence"), 0.0)
    signal_value = _position_sign(position) * confidence

    features = {
        "price":       _f(market.get("price")),
        "vwap":        _f(market.get("vwap")),
        "or_high":     _f(market.get("or_high")),
        "or_low":      _f(market.get("or_low")),
        "apmem_trend": _ap("apmem_trend"),
        "apmem_accel": _ap("apmem_accel"),
    }

    return {
        "signal":      signal_value,
        "position":    position,
        "confidence":  confidence,
        "vol_scale":   _ap("apmem_size_mult", 1.0),
        "current_vol": _ap("apmem_vol", 0.0),
        "features":    features,
    }
