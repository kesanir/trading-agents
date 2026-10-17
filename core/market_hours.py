"""
core/market_hours.py
Market session gating — pure time logic, no I/O.
Supports equities (NYSE), futures (23h), and option expiry cutoffs.
"""
from __future__ import annotations

from datetime import datetime, time, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# NYSE / options regular session
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# Options expiry cutoff is computed from settings (default 15 min before 4 PM)
def _options_expiry_cutoff() -> time:
    from core.config import settings
    return settings.expiry_cutoff_time

# Futures: CME Globex opens Sun 6 PM, closes Fri 5 PM; daily break 4–5 PM
_FUTURES_BREAK_START = time(16, 0)
_FUTURES_BREAK_END   = time(17, 0)


def now_et() -> datetime:
    return datetime.now(tz=ET)


def is_market_open() -> bool:
    """True if NYSE equities/options market is in regular session (Mon–Fri, 9:30–16:00 ET)."""
    now = now_et()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _MARKET_OPEN <= now.time() < _MARKET_CLOSE


def is_pre_market() -> bool:
    now = now_et()
    if now.weekday() >= 5:
        return False
    return time(4, 0) <= now.time() < _MARKET_OPEN


def is_after_hours() -> bool:
    now = now_et()
    if now.weekday() >= 5:
        return False
    return _MARKET_CLOSE <= now.time() < time(20, 0)


def is_futures_session() -> bool:
    """True if CME Globex is open (excludes daily 4–5 PM break and weekend close)."""
    now = now_et()
    wd = now.weekday()
    t = now.time()
    # Saturday: closed all day
    if wd == 5:
        return False
    # Sunday: opens at 6 PM
    if wd == 6:
        return t >= time(18, 0)
    # Friday: closes at 5 PM
    if wd == 4:
        return t < _FUTURES_BREAK_START
    # Mon–Thu: closed during daily break
    if _FUTURES_BREAK_START <= t < _FUTURES_BREAK_END:
        return False
    return True


def is_options_expiry_cutoff(expiry_date_str: str) -> bool:
    """
    True if we are past the options trading cutoff for a given expiry.
    expiry_date_str: YYYYMMDD
    """
    try:
        expiry = date(
            int(expiry_date_str[:4]),
            int(expiry_date_str[4:6]),
            int(expiry_date_str[6:8]),
        )
    except (ValueError, IndexError):
        return False
    now = now_et()
    if now.date() == expiry and now.time() >= _options_expiry_cutoff():
        return True
    if now.date() > expiry:
        return True
    return False


def minutes_to_close() -> int | None:
    """Minutes remaining in regular session, or None if market is closed."""
    if not is_market_open():
        return None
    now = now_et()
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return max(0, int((close_dt - now).total_seconds() / 60))


def session_label() -> str:
    if is_market_open():
        mins = minutes_to_close()
        return f"regular_session ({mins}m to close)"
    if is_pre_market():
        return "pre_market"
    if is_after_hours():
        return "after_hours"
    return "closed"
