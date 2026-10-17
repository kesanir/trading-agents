"""
signal_quality_report.py — Print signal accuracy dashboard.

Usage:
  python signal_quality_report.py
  python signal_quality_report.py --format json
  python signal_quality_report.py --bias LONG
  python signal_quality_report.py --trigger opening_range_breakout_long
"""
from __future__ import annotations

import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.signal_quality_service import build_signal_quality_report
from app.services.formatting_service import format_signal_quality_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal quality / accuracy dashboard.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--bias", help="Filter by bias (LONG|SHORT|NEUTRAL)")
    parser.add_argument("--trigger", help="Filter by trigger_type")
    args = parser.parse_args()

    report = build_signal_quality_report()

    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
        return

    print(format_signal_quality_report(report))


if __name__ == "__main__":
    main()
