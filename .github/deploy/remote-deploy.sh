#!/usr/bin/env bash
set -euo pipefail
expected_sha=$1
preflight_only=$2
[[ "$expected_sha" =~ ^[0-9a-f]{40}$ ]]
[[ "$preflight_only" = true || "$preflight_only" = false ]]
# SSM runs as root; hold an instance-wide lock even if a runner is cancelled.
exec 9>/run/lock/trading-agents-deploy.lock
flock -n 9 || { echo 'Another deployment holds the instance lock.' >&2; exit 1; }
timer=trading-after-open.timer
enabled=$(systemctl is-enabled "$timer" 2>/dev/null || true)
active=$(systemctl is-active "$timer" 2>/dev/null || true)
echo "$timer: enabled=$enabled active=$active"
service_state=$(systemctl show trading-after-open.service --property=ActiveState --value)
if [[ "$enabled" = disabled || "$enabled" = masked ]]; then
  [[ "$active" = inactive && ( "$service_state" = inactive || "$service_state" = failed ) ]] || {
    echo 'Deployment blocked: disabled after-open timer/service is not idle.' >&2; exit 1;
  }
elif [[ "$enabled" = enabled && "$active" = active ]]; then
  # Enabled schedules are allowed only after all installed reporting units have
  # been verified against their reviewed source, with explicit safe execution.
  python3 /opt/trading-agents/.github/deploy/verify-reporting-units.py
else
  echo 'Deployment blocked: unexpected after-open timer state.' >&2; exit 1
fi
# Do not add scripts/deploy_ec2.sh to Git: that path already exists untracked on
# EC2. Validate that existing local script before allowing it to execute.
python3 - <<'PY'
from pathlib import Path
expected = '''#!/usr/bin/env bash
set -euo pipefail
cd /opt/trading-agents
echo "Fetching latest code..."
git fetch origin main
echo "Checking for local changes..."
if ! git diff --quiet || ! git diff --cached --quiet; then
echo "ERROR: Local tracked changes detected. Deployment stopped."
exit 1
fi
echo "Updating application..."
git merge --ff-only origin/main
echo "Installing dependencies..."
.venv/bin/python -m pip install -r requirements.txt
echo "Checking dependencies..."
.venv/bin/python -m pip check
echo "Checking Python syntax..."
.venv/bin/python -m compileall -q daily_runner.py state_change_runner.py app core
echo "Deployment completed."
echo "Trading services were not restarted."
'''
def normalize(text):
    return [line.strip() for line in text.splitlines() if line.strip()]
script = Path('/opt/trading-agents/scripts/deploy_ec2.sh')
if script.is_symlink() or not script.is_file():
    raise SystemExit('Existing deployment script missing or symlinked; stopped.')
if normalize(script.read_text()) != normalize(expected):
    raise SystemExit('Existing deployment script differs from reviewed safe version; stopped.')
print('Existing deployment script matches reviewed safe version.')
PY
export EXPECTED_SHA="$expected_sha" PREFLIGHT_ONLY="$preflight_only"
timeout --kill-after=30s 1100s runuser -u ubuntu -- env EXPECTED_SHA="$expected_sha" PREFLIGHT_ONLY="$preflight_only" bash -s <<'TRADING_DEPLOY_USER'
set -euo pipefail
cd /opt/trading-agents
trap 'echo "Preflight/deployment failed at line $LINENO: $BASH_COMMAND" >&2' ERR
# Reporting units use this lock as ubuntu; never create it as root.
exec 8>app/state/reporting.lock
flock -w 900 8 || { echo 'A reporting job holds the shared lock.' >&2; exit 1; }
test "$(git symbolic-ref --short HEAD)" = main
origin_url=$(git remote get-url origin)
case "$origin_url" in
  https://github.com/kesanir/trading-agents|https://github.com/kesanir/trading-agents.git) ;;
  git@github.com:kesanir/trading-agents|git@github.com:kesanir/trading-agents.git) ;;
  ssh://git@github.com/kesanir/trading-agents|ssh://git@github.com/kesanir/trading-agents.git) ;;
  *) echo 'Unexpected origin URL; deployment stopped.' >&2; exit 1 ;;
esac
git diff --quiet
git diff --cached --quiet
test -x .venv/bin/python
git fetch origin main
test "$(git rev-parse origin/main)" = "$EXPECTED_SHA"
git merge-base --is-ancestor HEAD "$EXPECTED_SHA"
# Stop if a future commit would track .env or replace the host-local script.
test -z "$(git ls-tree -r --name-only "$EXPECTED_SHA" -- .env scripts/deploy_ec2.sh)"
if [[ "$PREFLIGHT_ONLY" = true ]]; then
  echo 'Preflight passed. No code or dependencies changed.'
  exit 0
fi
# Restrict the reviewed script's second fetch to this run's exact commit, and
# make Git reject collisions with ignored files as well as untracked files.
git() {
  case "${1:-}" in
    fetch)
      command git "$@" && test "$(command git rev-parse origin/main)" = "$EXPECTED_SHA"
      ;;
    merge)
      command git merge --ff-only --no-overwrite-ignore "$EXPECTED_SHA"
      ;;
    *) command git "$@" ;;
  esac
}
export -f git
bash /opt/trading-agents/scripts/deploy_ec2.sh
test "$(command git rev-parse HEAD)" = "$EXPECTED_SHA"
echo "Deployed commit: $EXPECTED_SHA"
TRADING_DEPLOY_USER
echo 'Trading services and timers were not restarted or enabled.'
