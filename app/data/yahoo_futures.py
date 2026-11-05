"""Timestamped Yahoo references for the morning map; never an execution feed."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math

ET = ZoneInfo('America/New_York')


def validate_reference(reference, now, max_age_minutes=30):
    stamp = datetime.fromisoformat(reference['timestamp'])
    if stamp.tzinfo is None:
        raise ValueError('Yahoo reference has no timezone')
    age = (now - stamp).total_seconds()
    if not -60 <= age <= max_age_minutes * 60:
        raise ValueError(f"Yahoo {reference['symbol']} reference is stale or in the future")
    if not math.isfinite(reference['price']) or reference['price'] <= 0:
        raise ValueError('Invalid Yahoo price')


def _reference(symbol, now):
    import yfinance as yf
    # Minute-bar closes carry an observable market timestamp, unlike fast_info.
    frame = yf.Ticker(symbol).history(period='5d', interval='1m', prepost=True,
                                     auto_adjust=False, actions=False, timeout=20)
    if frame.empty or frame.index.tz is None:
        raise RuntimeError(f'Yahoo {symbol}: no timestamped bars')
    frame = frame.sort_index().dropna(subset=['Close', 'High', 'Low'])
    frame = frame[frame.index <= now]
    if frame.empty:
        raise RuntimeError(f'Yahoo {symbol}: no usable bars')
    reference = {'symbol': symbol, 'price': float(frame.Close.iloc[-1]),
                 'timestamp': frame.index[-1].to_pydatetime().isoformat(),
                 'source': 'Yahoo Finance / 1-minute bar close',
                 'delay_notice': 'May be delayed; reference levels only'}
    validate_reference(reference, now)
    return reference, frame


def get_gamma_market_snapshot(now=None):
    now = now or datetime.now(timezone.utc)
    nq, bars = _reference('NQ=F', now)
    qqq, qqq_bars = _reference('QQQ', now)
    nq,bars,qqq=align_references(nq,bars,qqq,qqq_bars,now)
    if abs((datetime.fromisoformat(nq['timestamp']) -
            datetime.fromisoformat(qqq['timestamp'])).total_seconds()) > 300:
        raise RuntimeError('Yahoo QQQ and NQ references are more than five minutes apart')
    # Globex session begins at 18:00 ET on the prior calendar day (Sunday on Monday).
    today = now.astimezone(ET).date()
    start = datetime.combine(today - timedelta(days=1), datetime.min.time(), ET).replace(hour=18)
    overnight = bars[bars.index >= start]
    if overnight.empty:
        raise RuntimeError('Yahoo NQ: overnight session unavailable')
    volume = overnight.Volume.fillna(0).clip(lower=0)
    vwap = None
    if volume.sum() > 0:
        typical = (overnight.High + overnight.Low + overnight.Close) / 3
        vwap = float((typical * volume).sum() / volume.sum())
    return {'qqq': qqq, 'nq': nq, 'nq_overnight_high': float(overnight.High.max()),
            'nq_overnight_low': float(overnight.Low.min()), 'nq_globex_vwap': vwap,
            'overnight_start': start.isoformat(),
            'contract_notice': 'Yahoo NQ=F continuous reference; verify broker contract/basis. '
                               'NQ and MNQ share index-point references.'}


def align_references(nq,nq_bars,qqq,qqq_bars,now):
    """Use actual bars at a common cutoff when Yahoo futures lag equities.

    Never relabel a newer price with an older timestamp. Both selected prices
    still pass the 30-minute freshness policy and the five-minute skew limit.
    """
    cutoff=min(nq_bars.index[-1],qqq_bars.index[-1])
    aligned=[]
    frames=[]
    for reference,frame in ((nq,nq_bars),(qqq,qqq_bars)):
        frame=frame[frame.index<=cutoff]
        if frame.empty:
            raise RuntimeError('Yahoo has no bars at the common reference cutoff')
        selected={**reference,'latest_available_timestamp':reference['timestamp'],
                  'price':float(frame.Close.iloc[-1]),
                  'timestamp':frame.index[-1].to_pydatetime().isoformat(),
                  'alignment_notice':'Actual historical minute bars aligned to the slower Yahoo feed'}
        validate_reference(selected,now)
        aligned.append(selected)
        frames.append(frame)
    if abs((datetime.fromisoformat(aligned[0]['timestamp'])-
            datetime.fromisoformat(aligned[1]['timestamp'])).total_seconds())>300:
        raise RuntimeError('Yahoo QQQ and NQ references remain more than five minutes apart after alignment')
    return aligned[0],frames[0],aligned[1]
