"""Generate a SYNTHETIC example without network access, credentials or messages."""
import argparse
from datetime import datetime, timezone
from math import exp


def demo_report():
    from app.services.gamma_report_service import build_report
    from app.features.option_time import expiry_years
    now = datetime(2026, 11, 4, 13, 55, tzinfo=timezone.utc)
    quote = '2026-11-04T13:54:00+00:00'
    market = {'qqq': {'symbol':'QQQ','price':695.,'timestamp':quote,'source':'SYNTHETIC'},
              'nq': {'symbol':'NQ=F','price':28450.,'timestamp':quote,'source':'SYNTHETIC'},
              'nq_overnight_high':28510.,'nq_overnight_low':28180.,'nq_globex_vwap':28300.,
              'contract_notice':'SYNTHETIC example; not current market data'}
    contracts = []
    for expiry in ('2026-11-04','2026-11-06','2026-12-18'):
        for strike in range(660,731):
            for side in ('call','put'):
                center = 705 if side == 'call' else 685
                interest = int(100 + 5000*exp(-((strike-center)/5)**2))
                contracts.append({'symbol':f'{expiry}-{strike}-{side}','strike':strike,'side':side,
                                  'expiry':expiry,'oi':interest,'iv':.22,
                                  'years':expiry_years(expiry,now,symbol='QQQ'),
                                  'quote_timestamp':'2026-11-03T20:59:00+00:00','oi_date':'2026-11-03'})
    report = build_report(market,contracts,{'feed':'SYNTHETIC - NOT LIVE','loaded':len(contracts),
                                          'usable':len(contracts),'oi_coverage':1,
                                          'oldest_quote':'2026-11-03T20:59:00Z',
                                          'newest_quote':'2026-11-03T20:59:00Z',
                                          'oldest_oi_date':'2026-11-03'},now)
    report['demo'] = True
    return report


def main():
    from app.services.gamma_report_pdf import render_report
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    render_report(demo_report(),args.output)


if __name__ == '__main__':
    main()
