from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime

# This file is: app/services/trade_intent_service.py
# Project root is two levels up from here.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PREMARKET_DIR = PROJECT_ROOT / "app" / "reports" / "premarket"


def load_latest_premarket_intent(debug: bool = False) -> Optional[Dict[str, Any]]:
    if debug:
        print("PROJECT_ROOT:", PROJECT_ROOT)
        print("PREMARKET_DIR:", PREMARKET_DIR)

    if not PREMARKET_DIR.exists():
        if debug:
            print("Premarket dir does not exist")
        return None

    # Folders like: app/reports/premarket/2026-04-10
    date_dirs = sorted(
        [p for p in PREMARKET_DIR.iterdir() if p.is_dir()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if debug:
        print("Date dirs:", [p.name for p in date_dirs[:5]])

    if not date_dirs:
        if debug:
            print("No date dirs found")
        return None

    latest_date_dir = date_dirs[0]

    # Optional same-day guard
    today = str(datetime.now().date())
    if debug:
        print("Latest date dir:", latest_date_dir)
        print("Today:", today)

    if latest_date_dir.name != today:
        if debug:
            print("Latest date dir is not today")
        return None

    # Files like: 2026-04-10_22-12-53.json
    json_files = sorted(
        latest_date_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if debug:
        print("JSON files:", [p.name for p in json_files[:10]])

    if not json_files:
        if debug:
            print("No JSON files found")
        return None

    latest_file = json_files[0]

    if debug:
        print("Latest file:", latest_file)

    try:
        data = json.loads(latest_file.read_text(encoding="utf-8"))

        if debug:
            print("Top-level keys:", list(data.keys())[:20])

        # Case 1: raw plan saved directly
        intent = data.get("trade_intent")
        if intent:
            if debug:
                print("Found top-level trade_intent")
            return intent

        # Case 2: wrapped save format
        raw = data.get("raw", {})
        intent = raw.get("trade_intent")
        if intent:
            if debug:
                print("Found raw.trade_intent")
            return intent

        if debug:
            print("No trade_intent found in latest file")

    except Exception as e:
        if debug:
            print("Read error:", e)
        return None

    return None