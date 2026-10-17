from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


BASE_DIR = Path("app/reports")


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def _date_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _build_paths(report_type: str) -> Dict[str, Path]:
    day_dir = BASE_DIR / report_type / _date_str()
    _ensure_dir(day_dir)

    ts = _now_str()
    json_path = day_dir / f"{ts}.json"
    txt_path = day_dir / f"{ts}.txt"

    return {
        "json": json_path,
        "txt": txt_path,
        "dir": day_dir,
    }


def save_report(
    report_type: str,
    raw: Dict[str, Any],
    formatted: str,
) -> Dict[str, str]:
    """
    Saves both JSON and formatted text.
    """
    paths = _build_paths(report_type)

    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump(raw, f, indent=2, default=str)

    with open(paths["txt"], "w", encoding="utf-8") as f:
        f.write(formatted)

    return {
        "json_path": str(paths["json"]),
        "txt_path": str(paths["txt"]),
    }