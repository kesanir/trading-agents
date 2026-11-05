"""Existing Polygon account fallback, with honest timing and IV provenance."""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


def normalize_snapshot(rows, now):
    from app.features.option_time import expiry_years
    today=now.astimezone(ZoneInfo('America/New_York')).date()
    eligible=[]
    for row in rows:
        det=row.get('details') or {}
        try:
            expiry=datetime.strptime(det['expiration_date'],'%Y-%m-%d').date()
            if not today<=expiry<=today+timedelta(days=45):
                continue
            if float(det.get('shares_per_contract',100))!=100:
                continue
            strike=float(det['strike_price'])
            oi=float(row['open_interest'])
            if not all(isfinite(v) for v in (strike,oi)) or strike<=0 or oi<0:
                continue
            years=expiry_years(expiry,now,symbol='QQQ')
            if years<=0 or det['contract_type'] not in ('call','put'):
                continue
            eligible.append((row,det,strike,int(oi),years))
        except (KeyError,TypeError,ValueError):
            continue
    surface={}
    for row,det,strike,oi,years in eligible:
        iv=row.get('implied_volatility')
        if isinstance(iv,(int,float)) and isfinite(iv) and .001<iv<=3:
            surface.setdefault((det['expiration_date'],strike),[]).append(iv)
    contracts=[]
    for row,det,strike,oi,years in eligible:
        iv=row.get('implied_volatility')
        source='Polygon contract IV'
        if not isinstance(iv,(int,float)) or not isfinite(iv) or not .001<iv<=3:
            values=surface.get((det['expiration_date'],strike))
            if not values:
                continue
            iv=median(values)
            source='Polygon same-strike paired IV approximation'
        quote=row.get('last_quote') or {}
        stamp=None
        if quote.get('last_updated'):
            stamp=datetime.fromtimestamp(quote['last_updated']/1e9,timezone.utc).isoformat()
        contracts.append({'symbol':det['ticker'],'strike':strike,'expiry':det['expiration_date'],
                          'side':det['contract_type'],'oi':oi,'iv':iv,'years':years,
                          'iv_source':source,'quote_timestamp':stamp,'oi_date':None,
                          'bid':quote.get('bid'),'ask':quote.get('ask')})
    total_oi=sum(c[3] for c in eligible)
    usable_oi=sum(c['oi'] for c in contracts)
    oi_coverage=usable_oi/total_oi if total_oi else 0
    if len(eligible)<20 or len(eligible)/max(len(rows),1)<.98 or len(contracts)/len(eligible)<.8 or oi_coverage<.98:
        raise RuntimeError('Polygon fallback lacks adequate OI/IV coverage')
    return contracts,{'provider':'Polygon/Massive','feed':'snapshot; quote access not assumed',
                      'loaded':len(eligible),'usable':len(contracts),'oi_coverage':oi_coverage,
                      'coverage_basis':'Share of reported open interest represented, not quote freshness',
                      'oldest_quote':'not supplied','newest_quote':'not supplied',
                      'oldest_oi_date':'not supplied','model_only':True,
                      'paired_iv_contracts':sum('paired IV' in c['iv_source'] for c in contracts),
                      'notice':'MODEL ONLY: Polygon does not supply OI dates or complete quote timing on this plan. '
                               'All setups are WATCH ONLY until current OI and executable quotes are verified.'}


def collect_polygon_options(now):
    from app.data.polygon_client import _get
    today=now.astimezone(ZoneInfo('America/New_York')).date()
    url='https://api.polygon.io/v3/snapshot/options/QQQ'
    params={'limit':250,'expiration_date.gte':today.isoformat(),
            'expiration_date.lte':(today+timedelta(days=45)).isoformat()}
    rows=[]
    seen=set()
    while url:
        if url in seen or len(seen)>=100 or urlparse(url).hostname not in ('api.polygon.io','api.massive.com'):
            raise RuntimeError('Invalid Polygon pagination')
        seen.add(url)
        result=_get(url,params,log_errors=True)
        if result is None:
            raise RuntimeError('Polygon options page failed; partial chain rejected')
        rows.extend(result.get('results') or [])
        url=result.get('next_url')
        params={}
    return normalize_snapshot(rows,now)
