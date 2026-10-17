"""Verify installed reporting units before activation or enabled-timer deployment."""
from pathlib import Path
import subprocess

ROOT = Path('/opt/trading-agents')
INSTALLED = Path('/etc/systemd/system')
JOBS = ('after-open', 'state', 'mnq')


def value(unit, key):
    return subprocess.check_output(
        ['systemctl', 'show', unit, '--property=' + key, '--value'], text=True).strip()


def verify():
    for job in JOBS:
        for kind in ('service', 'timer'):
            unit = f'trading-{job}.{kind}'
            installed = INSTALLED / unit
            reference = ROOT / 'deploy/systemd' / unit
            if value(unit, 'FragmentPath') != str(installed) or value(unit, 'DropInPaths'):
                raise RuntimeError(f'{unit}: unexpected fragment or drop-in override')
            if installed.is_symlink() or installed.read_bytes() != reference.read_bytes():
                raise RuntimeError(f'{unit}: installed unit differs from reviewed source')
        service = f'trading-{job}.service'
        text = (ROOT / 'deploy/systemd' / service).read_text()
        expected = ('ExecStart=/usr/bin/flock -w 20 /opt/trading-agents/app/state/reporting.lock '
                    '/usr/bin/env ALPACA_PAPER=True DRY_RUN=True '
                    '/opt/trading-agents/.venv/bin/python reporting_job.py ' + job + ' --telegram')
        if expected not in text.splitlines() or value(service, 'User') != 'ubuntu':
            raise RuntimeError(f'{service}: reporting-only execution settings not verified')
    print('Installed reporting units match reviewed paper/dry-run services; no overrides.')


if __name__ == '__main__':
    verify()
