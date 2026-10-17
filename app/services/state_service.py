from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


STATE_DIR = Path("app/state")
STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_state(filename: str) -> Dict[str, Any]:
    path = STATE_DIR / filename
    if not path.exists():
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(filename: str, state: Dict[str, Any]) -> None:
    path = STATE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)