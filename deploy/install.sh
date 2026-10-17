#!/usr/bin/env bash
set -euo pipefail
cd /opt/trading-agents
mkdir -p app/state
exec 9>app/state/reporting.lock
flock -w 20 9 || { echo 'A reporting job or deployment holds the shared lock.' >&2; exit 1; }
# Stage reviewed units only. Installing must never start reporting or trading.
for job in after-open state mnq; do
  timer="trading-$job.timer"
  enabled=$(systemctl is-enabled "$timer" 2>/dev/null || true)
  active=$(systemctl is-active "$timer" 2>/dev/null || true)
  service=$(systemctl show "trading-$job.service" --property=ActiveState --value 2>/dev/null || true)
  case "$enabled" in disabled|masked|not-found|'') ;; *) echo "Disable $timer before staging units" >&2; exit 1;; esac
  case "$active" in inactive|unknown) ;; *) echo "Stop $timer before staging units" >&2; exit 1;; esac
  case "$service" in inactive|failed|'') ;; *) echo "Reporting service $job is not idle" >&2; exit 1;; esac
done
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s deployment_tests -v
ALPACA_PAPER=True DRY_RUN=True .venv/bin/python -c 'from app.services.deployment_safety import check_safety; check_safety()'
sudo -n systemd-analyze verify deploy/systemd/*
sudo -n install -m 644 deploy/systemd/* /etc/systemd/system/
sudo -n systemctl daemon-reload
echo 'Reporting units staged. No timers enabled and no services started.'
