import ast
import sys
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]

class SafetyTests(unittest.TestCase):
    def test_calendar_holiday_early_close_and_dst(self):
        from app.services.session_calendar import in_session
        et = ZoneInfo('America/New_York')
        for text, expected in [('2026-12-25T10:00', False), ('2026-11-27T12:59', True),
                               ('2026-11-27T13:00', False), ('2026-10-17T10:00', False),
                               ('2026-10-19T09:45', True), ('2026-11-02T09:45', True)]:
            self.assertEqual(in_session(now=datetime.fromisoformat(text).replace(tzinfo=et)), expected)

    def test_premarket_eight_eastern(self):
        from app.services.session_calendar import in_session, ET
        self.assertTrue(in_session(True, datetime(2026, 10, 19, 8, tzinfo=ET)))

    def test_fail_closed(self):
        from app.services.deployment_safety import check_safety
        for paper, dry in [(False, True), (True, False)]:
            config = types.ModuleType('core.config')
            config.settings = types.SimpleNamespace(ALPACA_PAPER=paper, DRY_RUN=dry)
            with patch.dict(sys.modules, {'core.config': config}), self.assertRaises(RuntimeError):
                check_safety()

    def test_after_open_cannot_reach_execution_in_dry_run(self):
        # Execute the actual function in isolation; any unexpected broker/signal import fails.
        tree = ast.parse((ROOT / 'daily_runner.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'run_after_open')
        module = ast.Module(body=[function], type_ignores=[])
        config = types.ModuleType('core.config')
        config.settings = types.SimpleNamespace(ALPACA_PAPER=True, DRY_RUN=True)
        apmem = types.ModuleType('app.services.intraday_apmem_service')
        apmem.build_intraday_apmem_signal = lambda **kw: types.SimpleNamespace(**{k: 'TEST' for k in
            ['apmem_state','prev_apmem_state','apmem_transition','apmem_signal','apmem_bias',
             'apmem_vol','apmem_trend','apmem_accel','apmem_size_mult']})
        signal = types.ModuleType('app.services.signal_file_service')
        forbidden_calls = []
        def forbidden(*a, **kw):
            forbidden_calls.append((a, kw))
            raise AssertionError('Execution/signal file reached')
        signal.write_signal_file = forbidden
        signal.build_signal_data = forbidden
        ns = dict(get_intraday_context=lambda *a, **kw: dict(price=1,vwap=1,or_high=2,or_low=0),
                  build_futures_trigger=lambda **kw: {}, format_futures_trigger=lambda t: 'report',
                  now_pacific=lambda: datetime.now(), save_report=lambda **kw: None,
                  send_telegram_message=forbidden)
        with patch.dict(sys.modules, {'core.config':config, apmem.__name__:apmem, signal.__name__:signal}):
            exec(compile(module, 'daily_runner.py', 'exec'), ns)
            ns['run_after_open'](nq_price=1)
            ns['run_after_open'](nq_price=1, auto_trade=True, auto_exit=True, write_signals=True, dry_run=False)
        self.assertEqual(forbidden_calls, [], 'Dry run must not attempt signal writes or Telegram sends')

    def test_compile_changed_python(self):
        for path in ['daily_runner.py','state_change_runner.py','reporting_job.py','mnq_monitor.py']:
            compile((ROOT/path).read_text(encoding='utf-8'), path, 'exec')

    def test_mnq_requires_consecutive_changes_and_stays_quiet_when_unchanged(self):
        import mnq_monitor
        feed = types.ModuleType('app.data.futures_ticks')
        current = {'price': 101}
        def context(**kwargs):
            bar = types.SimpleNamespace(to_pydatetime=lambda: datetime.now(timezone.utc))
            return dict(price=current['price'], vwap=100, or_high=105, or_low=95,
                        bars=types.SimpleNamespace(index=[bar]))
        feed.get_nq_intraday_context = context
        telegram = types.ModuleType('app.services.telegram_service')
        telegram.send_telegram_message = Mock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(mnq_monitor, 'STATE', Path(directory)/'state.json'), \
             patch('app.services.deployment_safety.check_safety'), \
             patch('app.services.session_calendar.in_session', return_value=True), \
             patch.dict(sys.modules, {feed.__name__:feed, telegram.__name__:telegram}):
            for price in (101, 101, 99, 101, 99):
                current['price'] = price
                mnq_monitor.run(True)
            self.assertEqual(telegram.send_telegram_message.call_count, 1)
            mnq_monitor.run(True)
            self.assertEqual(telegram.send_telegram_message.call_count, 2)
            mnq_monitor.run(True)
            self.assertEqual(telegram.send_telegram_message.call_count, 2)

    def test_mnq_rejects_stale_future_and_missing_data(self):
        import mnq_monitor
        feed = types.ModuleType('app.data.futures_ticks')
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(mnq_monitor, 'STATE', Path(directory)/'state.json'), \
             patch('app.services.deployment_safety.check_safety'), \
             patch('app.services.session_calendar.in_session', return_value=True), \
             patch.dict(sys.modules, {feed.__name__:feed}):
            for offset in (timedelta(minutes=-6), timedelta(minutes=1)):
                stamp = datetime.now(timezone.utc) + offset
                bar = types.SimpleNamespace(to_pydatetime=lambda: stamp)
                feed.get_nq_intraday_context = lambda **kw: {'bars':types.SimpleNamespace(index=[bar])}
                with self.assertRaises(RuntimeError):
                    mnq_monitor.run(True)
            feed.get_nq_intraday_context = lambda **kw: None
            with self.assertRaisesRegex(RuntimeError, 'no QQQ substitution'):
                mnq_monitor.run(True)
            self.assertFalse(mnq_monitor.STATE.exists())

    def test_reporting_wrapper_skips_closed_session_before_running_app(self):
        import reporting_job
        for job in ('after-open', 'state', 'mnq'):
            with patch.object(reporting_job, 'check_safety'), \
                 patch.object(reporting_job, 'in_session', return_value=False), \
                 patch.object(reporting_job.subprocess, 'run') as run, \
                 patch.object(sys, 'argv', ['reporting_job.py', job, '--telegram']):
                reporting_job.main()
                run.assert_not_called()

if __name__ == '__main__':
    unittest.main()

