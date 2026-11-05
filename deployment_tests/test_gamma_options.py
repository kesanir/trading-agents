import copy
from datetime import datetime, timezone
import unittest

from app.services.gamma_option_suggestions import build_option_suggestions, quote_debit
from gamma_report_demo import demo_report


class OptionSuggestionTests(unittest.TestCase):
    def test_listed_same_expiry_legs_direction_cap_and_payoff(self):
        report = demo_report()
        ideas = report['option_suggestions']['rows']
        self.assertTrue(ideas)
        contracts = {c['symbol']:c for c in report['contracts']}
        for idea in ideas:
            self.assertTrue(idea['available'])
            buy,sell = contracts[idea['buy_symbol']],contracts[idea['sell_symbol']]
            self.assertEqual(buy['expiry'],sell['expiry'])
            self.assertEqual(buy['expiry'],idea['expiry'])
            self.assertEqual(buy['side'],sell['side'])
            self.assertGreaterEqual(idea['dte'],1)
            self.assertLessEqual(idea['dte'],7)
            if idea['side']=='call':
                self.assertLess(buy['strike'],sell['strike'])
                self.assertAlmostEqual(idea['breakeven_at_cap'],buy['strike']+idea['max_debit'])
            else:
                self.assertGreater(buy['strike'],sell['strike'])
                self.assertAlmostEqual(idea['breakeven_at_cap'],buy['strike']-idea['max_debit'])
            self.assertAlmostEqual(idea['max_loss_at_cap'],idea['max_debit']*100)
            self.assertAlmostEqual(idea['max_profit_at_cap'],(idea['width']-idea['max_debit'])*100)
            self.assertGreaterEqual(idea['expiry_rr_at_cap'],1.5)
            self.assertIsNone(idea['quote']['debit'])

    def test_no_invented_legs_or_zero_dte_fallback(self):
        report = demo_report()
        now = datetime.fromisoformat(report['as_of'])
        zero_dte = [c for c in report['contracts'] if c['expiry']==report['date']]
        result = build_option_suggestions(report['playbook']['rows'],zero_dte,now,'opra')
        self.assertTrue(all(not i['available'] for i in result['rows']))
        no_interest = [{**c,'oi':0} for c in report['contracts']]
        result = build_option_suggestions(report['playbook']['rows'],no_interest,now,'opra')
        self.assertTrue(all(not i['available'] for i in result['rows']))

    def test_quote_uses_ask_minus_bid_and_flags_age_feed_and_invalid_legs(self):
        now = datetime(2026,11,4,14,tzinfo=timezone.utc)
        buy = {'bid':2.1,'ask':2.2,'quote_timestamp':'2026-11-04T13:59:00Z'}
        sell = {'bid':.6,'ask':.7,'quote_timestamp':'2026-11-04T13:59:00Z'}
        quote = quote_debit(buy,sell,5,now,'opra')
        self.assertEqual(quote['debit'],1.6)
        self.assertEqual(quote['status'],'recent snapshot')
        for leg in (buy,sell):
            leg['quote_timestamp'] = '2026-11-03T20:59:00Z'
        quote = quote_debit(buy,sell,5,now,'indicative')
        self.assertEqual(quote['debit'],1.6)
        self.assertIn('prior/stale',quote['status'])
        self.assertIn('indicative',quote['status'])
        buy['bid'] = 3
        self.assertIsNone(quote_debit(buy,sell,5,now,'opra')['debit'])
        buy['bid'] = 2.1
        sell['quote_timestamp'] = '2026-11-03T20:55:00Z'
        self.assertIsNone(quote_debit(buy,sell,5,now,'opra')['debit'])

    def test_expensive_snapshot_is_wait_not_current_entry_recommendation(self):
        report = demo_report()
        contracts = copy.deepcopy(report['contracts'])
        selected = report['option_suggestions']['rows'][0]
        for c in contracts:
            if c['symbol']==selected['buy_symbol']:
                c.update(bid=3.4,ask=3.5)
            elif c['symbol']==selected['sell_symbol']:
                c.update(bid=.1,ask=.2)
        ideas = build_option_suggestions(report['playbook']['rows'],contracts,
                                          datetime.fromisoformat(report['as_of']),'opra')['rows']
        self.assertTrue(ideas[0]['status'].startswith('WAIT'))
        self.assertGreater(ideas[0]['quote']['debit'],ideas[0]['max_debit'])


if __name__=='__main__':
    unittest.main()
