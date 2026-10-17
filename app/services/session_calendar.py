from datetime import datetime, time
from zoneinfo import ZoneInfo
from functools import lru_cache
import pandas_market_calendars as mcal

ET = ZoneInfo("America/New_York")

@lru_cache(maxsize=16)
def bounds(day):
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return row.market_open.to_pydatetime(), row.market_close.to_pydatetime()

def in_session(premarket=False, now=None):
    now = now or datetime.now(ET)
    session = bounds(now.astimezone(ET).date())
    if session is None:
        return False
    opening, closing = session
    if premarket:
        start = datetime.combine(now.astimezone(ET).date(), time(8), ET)
        return start <= now < opening
    return opening <= now < closing
