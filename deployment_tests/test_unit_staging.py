"""Exercise the real staging script with mocked host commands, never sudo."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix', 'Linux staging script')
class UnitStagingTests(unittest.TestCase):
    def test_installer_blocks_active_timer_and_never_activates_units(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root/'.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nexit 0\n')
            python.chmod(0o755)
            (root/'deploy/systemd').mkdir(parents=True)
            (root/'deploy/systemd/example.service').write_text('[Service]\nExecStart=/bin/true\n')
            script = (ROOT/'deploy/install.sh').read_text().replace(
                'cd /opt/trading-agents', 'cd "$TEST_ROOT"')
            script_path = root/'install.sh'
            script_path.write_text(script)
            log = root/'sudo.log'
            wrapper = '''
set -euo pipefail
systemctl() {
 case "$1" in
 is-enabled) echo disabled;;
 is-active) echo "$TEST_TIMER_STATE";;
 show) echo inactive;;
 *) return 1;;
 esac
}
sudo() { printf '%s\\n' "$*" >> "$TEST_SUDO_LOG"; }
export -f systemctl sudo
bash "$TEST_ROOT/install.sh"
'''
            env = dict(os.environ, TEST_ROOT=str(root), TEST_SUDO_LOG=str(log), TEST_TIMER_STATE='active')
            result = subprocess.run(['bash','-c',wrapper],env=env,capture_output=True,text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())
            env['TEST_TIMER_STATE'] = 'inactive'
            result = subprocess.run(['bash','-c',wrapper],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text()
            self.assertIn('systemd-analyze verify', commands)
            self.assertIn('systemctl daemon-reload', commands)
            self.assertNotIn('systemctl enable', commands)
            self.assertNotIn('systemctl start', commands)
            self.assertNotIn('systemctl restart', commands)
