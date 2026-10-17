"""
performance_report.py — Print trade performance report.

Usage:
  python performance_report.py                         # all-time
  python performance_report.py --week                  # last 7 days
  python performance_report.py --month                 # last 30 days
  python performance_report.py --last 20               # last 20 trades
  python performance_report.py --start 2026-01-01      # from date
  python performance_report.py --start 2026-01-01 --end 2026-03-31
  python performance_report.py --setup opening_range_breakout_long
  python performance_report.py --bias LONG
  python performance_report.py --format json           # raw JSON output
"""
from __future__ import annotations

import argparse
import json
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.performance_service import build_performance_report
from app.services.formatting_service import format_performance_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Trade performance report.")
    parser.add_argument("--start",  default=None, help="Start date ISO (e.g. 2026-01-15)")
    parser.add_argument("--end",    default=None, help="End date ISO (e.g. 2026-03-31)")
    parser.add_argument("--last",   type=int, default=None, help="Last N trades only")
    parser.add_argument("--week",   action="store_true", help="Last 7 days (shortcut)")
    parser.add_argument("--month",  action="store_true", help="Last 30 days (shortcut)")
    parser.add_argument("--setup",  default=None, help="Filter by setup type")
    parser.add_argument("--bias",   default=None, help="Filter by bias (LONG|SHORT|NEUTRAL)")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    start_date = args.start
    end_date   = args.end

    if args.week:
        start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    elif args.month:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    report = build_performance_report(
        start_date=start_date,
        end_date=end_date,
        last_n=args.last,
        setup=args.setup,
        bias=args.bias,
    )

    if args.format == "json":
        print(json.dumps(report, indent=2, default=str))
        return

    print(format_performance_report(report))


if __name__ == "__main__":
    main()
