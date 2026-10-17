#!/usr/bin/env bash
set -euo pipefail
cd /opt/trading-agents
revision="${1:?Pass a tested commit SHA}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
# Refuse to overwrite EC2-only changes. Never clean/reset runtime data or .env.
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo 'Tracked EC2 changes exist; review and commit them first'; exit 1; }
exec 9>app/state/reporting.lock
flock -w 900 9
git fetch origin main
git merge-base --is-ancestor "$revision" origin/main
git merge --ff-only "$revision"
bash deploy/install.sh
