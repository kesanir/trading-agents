#!/usr/bin/env bash
set -euo pipefail
cd /opt/trading-agents
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
ALPACA_PAPER=True DRY_RUN=True .venv/bin/python -c 'from app.services.deployment_safety import check_safety; check_safety()'
mkdir -p app/state
sudo install -m 644 deploy/systemd/* /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/trading-{after-open,state,mnq}.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now trading-after-open.timer trading-state.timer trading-mnq.timer
systemctl list-timers --all 'trading-*'
