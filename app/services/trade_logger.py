from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


LOG_DIR = Path("app/performance")
LOG_DIR.mkdir(parents=True, exist_ok=True)

SIGNALS_FILE = LOG_DIR / "signals.jsonl"
TRADES_FILE = LOG_DIR / "trades.jsonl"
SIGNAL_OUTCOMES_FILE = LOG_DIR / "signal_outcomes.jsonl"


def _make_signal_id(signal: Dict[str, Any]) -> str:
    """8-char deterministic ID from timestamp + bias + trigger_type."""
    key = f"{datetime.now().isoformat()}|{signal.get('bias')}|{signal.get('trigger_type')}"
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def log_signal(signal: Dict[str, Any]) -> str:
    """Append a signal row and return its signal_id."""
    signal_id = _make_signal_id(signal)
    row = {
        "signal_id": signal_id,
        "timestamp": datetime.now().isoformat(),
        **signal,
    }
    _append_jsonl(SIGNALS_FILE, row)
    return signal_id


def log_signal_outcome(outcome: Dict[str, Any]) -> None:
    """Append a resolved signal outcome (forward prices, directional accuracy)."""
    row = {
        "resolved_at": datetime.now().isoformat(),
        **outcome,
    }
    _append_jsonl(SIGNAL_OUTCOMES_FILE, row)


def log_trade(trade: Dict[str, Any]) -> None:
    row = {
        "timestamp": datetime.now().isoformat(),
        **trade,
    }
    _append_jsonl(TRADES_FILE, row)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []

    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def read_signals() -> List[Dict[str, Any]]:
    return read_jsonl(SIGNALS_FILE)


def read_trades() -> List[Dict[str, Any]]:
    return read_jsonl(TRADES_FILE)


def read_signal_outcomes() -> List[Dict[str, Any]]:
    return read_jsonl(SIGNAL_OUTCOMES_FILE)