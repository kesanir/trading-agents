#!/usr/bin/env bash
set -euo pipefail
[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]]
[[ "$PREFLIGHT_ONLY" = true || "$PREFLIGHT_ONLY" = false ]]
# JSON encoding avoids shell/SSM quoting bugs. No application secrets are read.
python3 - <<'PY'
import json, os, pathlib
script = pathlib.Path('.github/deploy/remote-deploy.sh').read_text()
command = 'bash -s -- ' + os.environ['GITHUB_SHA'] + ' ' + os.environ['PREFLIGHT_ONLY'] + " <<'TRADING_DEPLOY_REMOTE'\n" + script + '\nTRADING_DEPLOY_REMOTE'
pathlib.Path('ssm-parameters.json').write_text(json.dumps({
    'commands': [command], 'executionTimeout': ['1200']}))
PY
command_id=$(aws ssm send-command \
  --instance-ids "$EC2_INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --timeout-seconds 60 \
  --comment "trading-agents ${GITHUB_SHA}" \
  --parameters file://ssm-parameters.json \
  --query 'Command.CommandId' --output text)
echo "SSM command: $command_id"
echo "SSM command: $command_id" >> "$GITHUB_STEP_SUMMARY"
# The standard CLI waiter stops after about 100 seconds. Dependencies can take
# longer, so poll to a bounded deadline and tolerate initial SSM propagation.
deadline=$((SECONDS + 1350))
while (( SECONDS < deadline )); do
  if aws ssm get-command-invocation --command-id "$command_id" \
      --instance-id "$EC2_INSTANCE_ID" --output json > invocation.json 2> invocation-error.txt; then
    status=$(jq -r .Status invocation.json)
    case "$status" in
      Pending|InProgress|Delayed|Cancelling) ;;
      *)
        jq -r '.StandardOutputContent, .StandardErrorContent' invocation.json
        echo "SSM result: $status" | tee -a "$GITHUB_STEP_SUMMARY"
        test "$status" = Success
        test "$(jq -r .ResponseCode invocation.json)" = 0
        exit 0
        ;;
    esac
  elif ! grep -q InvocationDoesNotExist invocation-error.txt; then
    cat invocation-error.txt >&2
    exit 1
  fi
  sleep 10
done
echo 'Timed out waiting for SSM. Inspect the command before retrying.' >&2
exit 1
