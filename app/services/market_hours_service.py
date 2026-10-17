from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")


def now_pacific() -> datetime:
    return datetime.now(PACIFIC)


def is_premarket_window(
    start_hour: int = 6,
    start_minute: int = 0,
    end_hour: int = 6,
    end_minute: int = 29,
) -> bool:
    now = now_pacific().time()
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    return start <= now <= end


def is_market_open_window(
    start_hour: int = 6,
    start_minute: int = 30,
    end_hour: int = 13,
    end_minute: int = 0,
) -> bool:
    now = now_pacific().time()
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    return start <= now <= end


def is_trigger_window(
    start_hour: int = 6,
    start_minute: int = 30,
    end_hour: int = 10,
    end_minute: int = 30,
) -> bool:
    now = now_pacific().time()
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    return start <= now <= end