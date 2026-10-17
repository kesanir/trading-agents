import importlib
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


class PositionMonitorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Test-only credentials; all client creation and network calls are mocked.
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            'ENV_FILE': str(Path(directory)/'absent.env'),
            'ALPACA_API_KEY':'test_key', 'ALPACA_SECRET_KEY':'test_secret',
            'ALPACA_PAPER':'True', 'DRY_RUN':'True',
        }):
            cls.runner = importlib.import_module('app.services.trading_runner')

    async def test_dry_run_cannot_cancel_orders_even_via_helper(self):
        runner = self.runner
        with patch.object(runner.settings, 'DRY_RUN', True), patch.object(runner, 'cancel_order', new_callable=AsyncMock) as cancel:
            await runner._cancel_pending_for_symbols(Mock(), {'TEST'}, [{'symbol':'TEST', 'status':'new', 'id':'order1'}])
            cancel.assert_not_awaited()

    async def test_flagged_position_is_reported_without_cancels_or_closes(self):
        runner = self.runner
        assessment = types.SimpleNamespace(should_close=True, symbol='TEST', details='test risk')
        with patch.object(runner.settings, 'DRY_RUN', True), \
             patch.object(runner, 'get_trading_client', return_value=Mock()), \
             patch.object(runner, 'init_db', new_callable=AsyncMock), \
             patch.object(runner, 'fetch_account', new_callable=AsyncMock, return_value={'equity':1,'buying_power':1}), \
             patch.object(runner, 'fetch_all_positions', new_callable=AsyncMock, return_value=[{}]), \
             patch.object(runner, 'fetch_open_orders', new_callable=AsyncMock, return_value=[{'symbol':'TEST','status':'new','id':'order1'}]), \
             patch.object(runner, 'parse_all_positions', return_value=[Mock()]), \
             patch.object(runner, 'detect_structures', return_value=([],[Mock()])), \
             patch.object(runner, 'evaluate_single_position', return_value=assessment), \
             patch.object(runner, '_log_assessment'), \
             patch.object(runner, '_symbols_for_close', return_value={'TEST'}), \
             patch.object(runner, 'is_market_open', return_value=True), \
             patch.object(runner, 'session_label', return_value='test'), \
             patch.object(runner, '_cancel_pending_for_symbols', new_callable=AsyncMock) as cancel, \
             patch.object(runner, 'execute_risk_assessment', new_callable=AsyncMock) as close, \
             patch.object(runner, 'save_run_record', new_callable=AsyncMock) as save:
            report = await runner.run_once()
            cancel.assert_not_awaited()
            close.assert_not_awaited()
            self.assertEqual(report.actions_taken, [{'symbol':'TEST','action':'would_close','dry_run':True,'details':'test risk'}])
            save.assert_awaited_once()


class PositionMonitorEntryTests(unittest.TestCase):
    def test_closed_session_never_imports_or_runs_the_position_monitor(self):
        import position_monitor_job as job
        with patch.object(job, 'check_safety') as safety, \
             patch.object(job, 'in_session', return_value=False), \
             patch.object(job.asyncio, 'run') as run:
            job.main()
            safety.assert_called_once()
            run.assert_not_called()
