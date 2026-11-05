"""Conditional QQQ debit verticals from listed contracts, never broker orders.

Payoff references: OCC/OIC bull-call and bear-put debit-spread strategies:
https://www.optionseducation.org/strategies/all-strategies/bull-call-spread-debit-call-spread
https://www.optionseducation.org/strategies/all-strategies/bear-put-spread
"""
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from math import isfinite
from zoneinfo import ZoneInfo


def quote_debit(long_leg, short_leg, width, now, feed):
    """Conservative snapshot ask(long)-bid(short), with explicit age/feed labels."""
    try:
        for leg in (long_leg, short_leg):
            bid, ask = float(leg['bid']), float(leg['ask'])
            if not all(isfinite(v) for v in (bid,ask)) or not 0 < bid <= ask:
                raise ValueError('Invalid bid/ask')
        stamps = [datetime.fromisoformat(c['quote_timestamp'].replace('Z','+00:00'))
                  for c in (long_leg,short_leg)]
        if any(t.tzinfo is None or t > now for t in stamps):
            raise ValueError('Invalid quote timestamp')
        debit = round(float(long_leg['ask'])-float(short_leg['bid']),2)
        if not 0 < debit < width:
            raise ValueError('Snapshot debit is outside spread bounds')
        if abs((stamps[0]-stamps[1]).total_seconds()) > 60:
            return {'debit':None,'status':'N/A: leg timestamps differ by more than 60 seconds'}
        recent = max((now-t).total_seconds() for t in stamps) <= 15*60
        label = 'recent snapshot' if recent else 'prior/stale snapshot'
        if feed.lower() != 'opra':
            label += '; indicative feed'
        return {'debit':debit,'status':label,'as_of':[t.isoformat() for t in stamps]}
    except (KeyError,TypeError,ValueError,AttributeError):
        return {'debit':None,'status':'N/A: no valid two-sided leg quotes'}


def build_option_suggestions(rows, contracts, now, feed):
    """Nearest listed 1-7 calendar DTE; 1-10 point debit verticals, same expiry.

    Max debit is a payoff-based ceiling, not a fair-value estimate or current ask.
    Width / 2.5 gives at least 1.5 maximum reward/risk at expiration before fees.
    """
    today = now.astimezone(ZoneInfo('America/New_York')).date()
    expiries = sorted({c['expiry'] for c in contracts
                       if 1 <= (date.fromisoformat(c['expiry'])-today).days <= 7})
    result = []
    for row in rows:
        idea = {'trigger':row['qqq_trigger'],'label':row['label'],'direction':row['direction'],
                'invalidation':f"QQQ 5-minute close {'below' if row['direction']=='long' else 'above'} "
                               f"{row['qqq_trigger']:.2f}; close both legs together.",
                'available':False,'status':'No suitable listed 1-7 DTE spread'}
        side = 'call' if row['direction']=='long' else 'put'
        direction = 1 if side=='call' else -1
        targets = row['qqq_targets']
        if not expiries or not targets:
            result.append(idea)
            continue
        target = targets[-1]
        for expiry in expiries:
            pool = [c for c in contracts if c['expiry']==expiry and c['side']==side and c['oi']>0]
            if len(pool) < 2:
                continue
            long_leg = min(pool,key=lambda c:abs(c['strike']-row['qqq_trigger']))
            if abs(long_leg['strike']-row['qqq_trigger']) > 1:
                continue
            shorts = [c for c in pool if 1 <= direction*(c['strike']-long_leg['strike']) <= 10
                      and direction*(target-c['strike']) >= 0]
            if not shorts:
                continue
            short_leg = min(shorts,key=lambda c:(abs(abs(c['strike']-long_leg['strike'])-5),
                                                 abs(target-c['strike'])))
            width = Decimal(str(abs(short_leg['strike']-long_leg['strike'])))
            cap = (width/Decimal('2.5')).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
            if cap <= 0:
                continue
            quote = quote_debit(long_leg,short_leg,float(width),now,feed)
            status = 'Conditional; refresh quotes when the trigger confirms'
            if quote['debit'] is not None and quote['debit'] > float(cap):
                status = 'WAIT: snapshot debit exceeds ceiling; reprice on trigger'
            idea.update({'available':True,'strategy':'Bull call debit' if side=='call' else 'Bear put debit',
                         'expiry':expiry,'dte':(date.fromisoformat(expiry)-today).days,
                         'side':side,'buy_symbol':long_leg['symbol'],'sell_symbol':short_leg['symbol'],
                         'buy_strike':long_leg['strike'],'sell_strike':short_leg['strike'],
                         'width':float(width),'max_debit':float(cap),
                         'max_loss_at_cap':float(cap*100),'max_profit_at_cap':float((width-cap)*100),
                         'expiry_rr_at_cap':float((width-cap)/cap),
                         'breakeven_at_cap':float(Decimal(str(long_leg['strike']))+direction*cap),
                         'full_value_beyond':short_leg['strike'],'quote':quote,'status':status})
            break
        result.append(idea)
    return {'rows':result,'expiry_policy':'Nearest suitable listed 1-7 calendar DTE; no automatic 0DTE fallback.',
            'pricing_note':'Debit ceiling is a condition, not a quote. Snapshot debit uses long ask minus short bid; '
                           'prior/indicative quotes are not executable. Reprice both legs when the trigger confirms.',
            'risk_note':'One standard QQQ vertical = 100-share multiplier. Loss/profit and breakeven are at the '
                        'debit ceiling, at expiration, before fees. Maximum profit requires price beyond the short '
                        'strike at expiration; hitting an intraday target does not lock it in. Close the intact spread '
                        'before expiration to avoid exercise/assignment exposure. Futures R/R is a separate test.'}
