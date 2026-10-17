import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
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

if __name__ == '__main__':
    unittest.main()

