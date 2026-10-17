import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class InstalledUnitsTests(unittest.TestCase):
    def test_verification_accepts_safe_units_and_rejects_overrides_or_changes(self):
        spec = importlib.util.spec_from_file_location('unit_verifier', ROOT/'.github/deploy/verify-reporting-units.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            for source in (ROOT/'deploy/systemd').iterdir():
                (installed/source.name).write_bytes(source.read_bytes())
            override = {'value': ''}
            def show(command, **kwargs):
                unit = command[2]
                key = command[3].split('=', 1)[1]
                return {'FragmentPath':str(installed/unit), 'DropInPaths':override['value'], 'User':'ubuntu',
                        'LoadState':'loaded' if (installed/unit).exists() else 'not-found'}[key]
            with patch.object(module, 'INSTALLED', installed), patch.object(module, 'ROOT', ROOT), \
                 patch.object(module.subprocess, 'check_output', side_effect=show):
                module.verify()
                override['value'] = '/etc/systemd/system/unsafe.conf'
                with self.assertRaisesRegex(RuntimeError, 'drop-in'):
                    module.verify()
                override['value'] = ''
                service = installed/'trading-after-open.service'
                service.write_text(service.read_text().replace('DRY_RUN=True', 'DRY_RUN=False'))
                with self.assertRaisesRegex(RuntimeError, 'differs'):
                    module.verify()
