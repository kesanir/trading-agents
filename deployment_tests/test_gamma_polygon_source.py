import copy
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.services.gamma_polygon_source import normalize_snapshot
from app.services.gamma_report_service import build_report
from gamma_report_demo import demo_report


class PolygonFallbackTests(unittest.TestCase):
    def rows(self):
        return [{'details':{'ticker':f'QQQ-{k}-{s}','expiration_date':'2026-11-06',
                            'contract_type':s,'shares_per_contract':100,'strike_price':k},
                 'open_interest':100,'implied_volatility':.22 if s=='put' else None}
                for k in range(685,706) for s in ('call','put')]

    def test_paired_iv_keeps_provenance_and_never_invents_timestamps(self):
        now=datetime(2026,11,4,13,55,tzinfo=timezone.utc)
        contracts,quality=normalize_snapshot(self.rows(),now)
        self.assertEqual(len(contracts),42)
        self.assertEqual(quality['oi_coverage'],1)
        self.assertEqual(quality['paired_iv_contracts'],21)
        self.assertTrue(quality['model_only'])
        self.assertTrue(all(c['oi_date'] is None and c['quote_timestamp'] is None for c in contracts))
        self.assertIn('paired IV',contracts[0]['iv_source'])
        for row in self.rows():
            row['implied_volatility']=None

    def test_material_missing_iv_or_oi_fails_closed(self):
        now=datetime(2026,11,4,13,55,tzinfo=timezone.utc)
        rows=self.rows()
        for row in rows:
            row['implied_volatility']=None
        with self.assertRaisesRegex(RuntimeError,'coverage'):
            normalize_snapshot(rows,now)
        rows=self.rows()
        rows[0]['open_interest']=None
        with self.assertRaisesRegex(RuntimeError,'coverage'):
            normalize_snapshot(rows,now)

    def test_model_only_report_is_watch_only_and_fits_two_pages(self):
        from app.services.gamma_report_pdf import render_report,summary
        from pypdf import PdfReader
        demo=demo_report()
        now=datetime.fromisoformat(demo['as_of'])
        contracts,quality=normalize_snapshot(self.rows(),now)
        report=build_report(demo['market'],contracts,quality,now)
        self.assertTrue(all(r['status'].startswith('WATCH ONLY') for r in report['playbook']['rows']))
        self.assertTrue(all(r['status'].startswith('WATCH ONLY') for r in report['option_suggestions']['rows']))
        self.assertIn('MODEL ONLY',summary(report))
        with tempfile.TemporaryDirectory() as directory:
            path=render_report(report,Path(directory)/'model-only.pdf')
            reader=PdfReader(path)
            self.assertEqual(len(reader.pages),2)
            self.assertIn('MODEL ONLY',' '.join(p.extract_text() for p in reader.pages))

    def test_fallback_runs_only_after_primary_failure(self):
        from app.services.gamma_report_service import collect_report
        demo=demo_report()
        now=datetime.fromisoformat(demo['as_of'])
        fallback=normalize_snapshot(self.rows(),now)
        with patch('app.data.yahoo_futures.get_gamma_market_snapshot',return_value=demo['market']), \
             patch('app.services.gamma_report_service.collect_options',side_effect=RuntimeError('stale OI')), \
             patch('app.services.gamma_polygon_source.collect_polygon_options',return_value=fallback) as source, \
             patch.dict('os.environ',{'POLYGON_API_KEY':'test-only'}):
            report=collect_report(now)
            source.assert_called_once_with(now)
            self.assertEqual(report['quality']['fallback_reason'],'stale OI')


if __name__=='__main__':
    unittest.main()
