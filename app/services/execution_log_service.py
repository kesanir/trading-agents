from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXECUTION_DIR = PROJECT_ROOT / "app" / "reports" / "executions"


def _today_file():
    d = datetime.now().strftime("%Y-%m-%d")
    path = EXECUTION_DIR / d
    path.mkdir(parents=True, exist_ok=True)
    return path / "execution_log.json"


def load_log() -> List[Dict[str, Any]]:
    path = _today_file()
    if not path.exists():
        return []
    return json.loads(path.read_text())


def append_log(entry: Dict[str, Any]):
    data = load_log()
    data.append(entry)
    _today_file().write_text(json.dumps(data, indent=2))


def already_executed(
    ticker: str,
    direction: str,
    lookback_minutes: int = 120,
) -> bool:
    logs = load_log()
    now = datetime.now()

    for row in reversed(logs):
        if row.get("ticker") != ticker:
            continue

        if row.get("direction") != direction:
            continue

        ts = datetime.fromisoformat(row["timestamp"])

        # time-based dedupe
        if now - ts < timedelta(minutes=lookback_minutes):
            return True

    return False