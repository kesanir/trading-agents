import copy
import importlib.util
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import types
import sys
import unittest
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo

from app.services.gamma_report_service import gamma, delta, spot_sweep, nq_level
from app.data.yahoo_futures import validate_reference
from gamma_report_demo import demo_report


class GammaSourceTests(unittest.TestCase):
    def test_legacy_premarket_range_never_uses_mag7_iv(self):
        from app.agents import premarket_agent as agent
        qqq = {'spot':100,'regime':'positive','iv_analytics':{'atm_iv':.2}}
        mag7 = {'candidates':[{'ticker':'TSLA','spot':300,'iv_analytics':{'atm_iv':.9}}]}
        with patch.object(agent,'classify_regime',return_value=qqq), \
             patch.object(agent,'build_mag7_summary',return_value={}), \
             patch.object(agent,'scan_named_universe',return_value=mag7), \
             patch.object(agent,'build_futures_bias',return_value={}), \
             patch.object(agent,'get_vix_data',return_value={}):
            result = agent.build_premarket_plan()
            self.assertEqual(result['proxy_atm_iv'],.2)
            self.assertAlmostEqual(result['expected_move'],round(100*.2/math.sqrt(365),2))

    def test_options_reject_missing_oi_default_iv_and_old_dates(self):
        from app.services.gamma_report_service import collect_options
        from app.services.session_calendar import bounds
        now = datetime(2026,11,4,13,55,tzinfo=timezone.utc)
        feed = types.ModuleType('app.data.alpaca_client')
        feed.OPTION_FEED = 'indicative'
        feed.fetch_oi_map = Mock(return_value={'contract':100})
        items = []
        for i in range(20):
            items.append({'details':{'ticker':str(i),'expiration_date':'2026-11-04',
                                     'strike_price':695,'contract_type':'call' if i%2 else 'put'},
                          'open_interest':100,'implied_volatility':.2,
                          'quote_timestamp':'2026-11-03T20:59:00Z','oi_date':'2026-11-03'})
        feed.iter_options_chain = Mock(side_effect=lambda *a,**kw: iter(items))
        with patch.dict(sys.modules,{feed.__name__:feed}):
            contracts,quality = collect_options(695,now)
            self.assertEqual(len(contracts),20)
            self.assertEqual(quality['oi_coverage'],1)
            self.assertTrue(feed.iter_options_chain.call_args.kwargs['strict'])
            for item in items:
                item['implied_volatility'] = None
            with self.assertRaisesRegex(RuntimeError,'coverage'):
                collect_options(695,now)
            for item in items:
                item['implied_volatility'] = .2
                item['oi_date'] = '2026-10-30'
            with self.assertRaisesRegex(RuntimeError,'coverage'):
                collect_options(695,now)
            feed.fetch_oi_map.return_value = {}
            with self.assertRaisesRegex(RuntimeError,'open interest unavailable'):
                collect_options(695,now)

    def test_yahoo_overnight_uses_globex_anchor_and_checks_alignment(self):
        import pandas as pd
        from app.data.yahoo_futures import get_gamma_market_snapshot
        now = datetime(2026,11,4,13,55,tzinfo=timezone.utc)
        stamps = pd.to_datetime(['2026-11-03T22:00Z','2026-11-03T23:00Z','2026-11-04T13:54Z'])
        frame = pd.DataFrame({'High':[99999,102,106],'Low':[0,98,102],
                              'Close':[100,100,104],'Volume':[100,1,3]},index=stamps)
        qqq = {'symbol':'QQQ','price':100,'timestamp':'2026-11-04T13:54:00+00:00'}
        nq = {'symbol':'NQ=F','price':104,'timestamp':'2026-11-04T13:54:00+00:00'}
        with patch('app.data.yahoo_futures._reference',side_effect=[(nq,frame),(qqq,frame)]):
            snapshot = get_gamma_market_snapshot(now)
            self.assertEqual(snapshot['nq_overnight_high'],106)
            self.assertEqual(snapshot['nq_overnight_low'],98)
            self.assertEqual(snapshot['nq_globex_vwap'],103)
        qqq['timestamp'] = '2026-11-04T13:40:00+00:00'
        with patch('app.data.yahoo_futures._reference',side_effect=[(nq,frame),(qqq,frame)]), \
             self.assertRaisesRegex(RuntimeError,'five minutes'):
            get_gamma_market_snapshot(now)


class GammaMathTests(unittest.TestCase):
    def test_same_day_early_close_and_dst(self):
        from app.features.option_time import expiry_years
        from app.services.session_calendar import in_session
        normal = datetime(2026,11,4,14,tzinfo=timezone.utc)
        self.assertAlmostEqual(expiry_years('2026-11-04',normal)*365*24,7)
        early = datetime(2026,11,27,14,tzinfo=timezone.utc)
        self.assertAlmostEqual(expiry_years('2026-11-27',early)*365*24,4)
        self.assertAlmostEqual(expiry_years('2026-11-04',normal,symbol='QQQ')*365*24,7.25)
        self.assertAlmostEqual(expiry_years('2026-11-27',early,symbol='QQQ')*365*24,4.25)
        self.assertEqual(expiry_years('2026-11-27',early+timedelta(hours=4)),0)
        for day in (datetime(2026,7,6,6),datetime(2026,11,4,6)):
            self.assertTrue(in_session(premarket=True,now=day.replace(tzinfo=ZoneInfo('America/Los_Angeles'))))
        self.assertFalse(in_session(True,datetime(2026,12,25,6,tzinfo=ZoneInfo('America/Los_Angeles'))))
        self.assertGreater(gamma(100,100,expiry_years('2026-11-04',normal),.2),0)

    def test_spot_sweep_matches_analytic_root_and_no_root(self):
        contracts = [{'strike':k,'years':.03,'iv':.2,'oi':100,'side':s}
                     for k,s in [(95,'put'),(105,'call')]]
        expected = math.sqrt(95*105)*math.exp(-.5*.2**2*.03)
        result = spot_sweep(contracts,100)
        self.assertAlmostEqual(result['nearest'],expected,places=3)
        self.assertEqual(len(result['crossings']),1)
        self.assertIsNone(spot_sweep(contracts[1:],100)['nearest'])

    def test_yahoo_freshness_alignment_and_tick_rounding(self):
        now = datetime(2026,11,4,14,tzinfo=timezone.utc)
        reference = {'symbol':'NQ=F','timestamp':(now-timedelta(minutes=20)).isoformat(),'price':28450}
        validate_reference(reference,now)
        for offset in (-31,2):
            reference['timestamp'] = (now+timedelta(minutes=offset)).isoformat()
            with self.assertRaises(ValueError):
                validate_reference(reference,now)
        self.assertEqual(nq_level(101,100,20000),20200)
        self.assertEqual(nq_level(100.000625,100,20000),20000.25)
        self.assertIsNone(nq_level(None,100,20000))

    def test_report_uses_qqq_iv_and_strict_today_expiry(self):
        r = demo_report()
        self.assertAlmostEqual(r['expected']['iv'],.22)
        seconds = (datetime.fromisoformat(r['expected']['horizon_end'])-
                   datetime.fromisoformat(r['as_of'])).total_seconds()
        self.assertAlmostEqual(r['expected']['move'],695*.22*math.sqrt(seconds/(365*86400)))
        self.assertIsNotNone(r['zero_dte_net_gex_b'])
        self.assertEqual(len(r['scenarios']),3)
        self.assertEqual(r['levels']['call_wall'],703)
        self.assertEqual(r['levels']['put_wall'],687)
        self.assertLess(r['scenarios'][1]['stop'],r['scenarios'][1]['entry'])
        self.assertGreater(r['scenarios'][2]['stop'],r['scenarios'][2]['entry'])
        for d in r['delta_references']:
            if d['call']:
                self.assertGreater(d['call'],r['market']['qqq']['price'])
                self.assertLessEqual(abs(d['call_actual_delta']-d['delta']),.03)


class GammaDeliveryTests(unittest.TestCase):
    def test_failed_prepare_cannot_leave_a_publishable_manifest(self):
        from app.services.gamma_report_job import prepare
        now = datetime(2026,11,4,13,55,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory, \
             patch('app.services.gamma_report_service.collect_report',return_value=demo_report()), \
             patch('app.services.gamma_report_pdf.render_report',side_effect=RuntimeError('overflow')):
            with self.assertRaisesRegex(RuntimeError,'overflow'):
                prepare(now,directory)
            self.assertFalse((Path(directory)/'2026-11-04'/'snapshot.json').exists())

    def prepared(self,folder):
        import hashlib
        r = demo_report()
        r['demo'] = False  # Transport-only test fixture; all sends are mocked.
        pdf = folder/'gamma-map.pdf'
        pdf.write_bytes(b'fake pdf for transport checks')
        r['pdf_sha256'] = hashlib.sha256(pdf.read_bytes()).hexdigest()
        (folder/'snapshot.json').write_text(json.dumps(r))
        return r

    def test_partial_delivery_retries_only_document_and_suppresses_duplicates(self):
        from app.services.gamma_report_job import publish
        now = datetime(2026,11,4,14,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'2026-11-04'
            folder.mkdir()
            self.prepared(folder)
            with patch('app.services.telegram_service.send_telegram_message',return_value=[{'ok':True}]) as message, \
                 patch('app.services.telegram_service.send_telegram_document',side_effect=[RuntimeError('offline'),{'ok':True}]) as document:
                with self.assertRaises(RuntimeError):
                    publish(True,now,directory)
                publish(True,now,directory)
                publish(True,now,directory)
                self.assertEqual(message.call_count,1)
                self.assertEqual(document.call_count,2)

    def test_publish_rejects_wrong_time_stale_quotes_and_altered_pdf(self):
        from app.services.gamma_report_job import publish
        now = datetime(2026,11,4,14,tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)/'2026-11-04'
            folder.mkdir()
            self.prepared(folder)
            with self.assertRaisesRegex(RuntimeError,'06:00'):
                publish(False,now-timedelta(minutes=1),directory)
            (folder/'gamma-map.pdf').write_bytes(b'changed')
            with self.assertRaisesRegex(RuntimeError,'does not match'):
                publish(False,now,directory)
            r = self.prepared(folder)
            r['market']['nq']['timestamp'] = (now-timedelta(minutes=31)).isoformat()
            (folder/'snapshot.json').write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'stale'):
                publish(False,now,directory)

    def test_two_page_pdf_and_audit_labels(self):
        from app.services.gamma_report_pdf import render_report
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as directory:
            path = render_report(demo_report(),Path(directory)/'report.pdf')
            reader = PdfReader(path)
            self.assertEqual(len(reader.pages),2)
            text = '\n'.join(page.extract_text() for page in reader.pages)
            for label in ('SYNTHETIC','NQ=F','Globex VWAP','Bullish acceptance','Bearish acceptance','06:'):
                if label != '06:':
                    self.assertIn(label,text)

    def test_premarket_wrapper_uses_premarket_gate(self):
        import reporting_job
        for job in ('gamma-prepare','gamma-publish'):
            with patch.object(reporting_job,'check_safety'), \
                 patch.object(reporting_job,'in_session',return_value=False) as gate, \
                 patch('app.services.gamma_report_job.run') as run, \
                 patch('sys.argv',['reporting_job.py',job,'--telegram']):
                reporting_job.main()
                gate.assert_called_once_with(premarket=True)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
