"""Exchange-aware remaining option life, including same-day and early closes."""
from datetime import date, datetime, timezone, timedelta
from app.services.session_calendar import bounds


def expiry_years(expiry, now=None, symbol=''):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Option valuation time must be timezone-aware')
    session = bounds(date.fromisoformat(str(expiry)))
    if session is None:
        return 0.0
    close = session[1]
    # QQQ options trade 15 minutes beyond the cash session, including early closes.
    # https://www.nasdaqtrader.com/Trader.aspx?id=optionshours
    if symbol.upper() == 'QQQ':
        close += timedelta(minutes=15)
    return max(0.0, (close - now).total_seconds()) / (365 * 86400)
