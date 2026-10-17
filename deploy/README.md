# Reporting safety deployment

The reporting code is deployed and the three reviewed paper/dry-run schedules
were activated on October 17, 2026. Premarket and intraday APMEM schedules remain
enabled. The legacy state-change timer and old MNQ monitor remain off to avoid
overlap. Normal code deployments run through OIDC/SSM after the safety tests pass;
they do not enable timers, restart services, submit orders or close positions.

## Reporting behavior

- After-open analysis: 09:45 America/New_York; a 15-minute opening range.
- State monitor: every two minutes during the regular US equity session, with
  two-run confirmation and a ten-minute alert cooldown. Unchanged state is quiet.
- MNQ monitor: NQ.c.0 proxy analysis during US equity regular hours only. Requires
  fresh Databento bars; stale/missing data fail visibly with no QQQ substitution.
  Two consecutive changed observations confirm an alert; unchanged state is quiet.
- NYSE holidays and early closes gate the reporting entry points. Calendar errors
  fail closed. Existing premarket timing is retained, including 08:00 Eastern.
- Reporting requires paper and dry-run settings. Units force both in the process
  environment, even if .env contains conflicting values. Entry, exit, and
  NinjaTrader signal-file flags remain separate and suppressed by dry run.
- Reporting jobs share app/state/reporting.lock with the OIDC deployment wrapper.
  Journald records stdout/stderr; job timeouts bound execution.

## Code deployment

The sole deployment path is `.github/workflows/deploy-ec2.yml` using the existing
AWS_REGION, EC2_INSTANCE_ID, and AWS_DEPLOY_ROLE_ARN repository variables. No SSH
deployment secrets or ec2-reporting GitHub environment are required. The older
deploy/update.sh entry point refuses to run.

PR checks exercise reporting safety without API calls or messages. On main,
the deployment workflow repeats the suite before obtaining AWS credentials.
The EC2 wrapper verifies the after-open timer remains disabled/inactive and its
service idle; verifies the existing untracked scripts/deploy_ec2.sh; rejects
tracked changes and untracked/ignored collisions; and updates by fast-forward to
the exact tested commit. It preserves .env and reports and restarts no services.

The deployment guard accepts an enabled after-open timer only when all three
installed reporting unit pairs match their reviewed source and have no systemd
drop-in overrides. It verifies explicit paper/dry-run execution and shares the
reporting lock before updating code. Disabled timers must be inactive.

## Stage units after code deployment

First inspect existing schedules and idle states in a trusted EC2 session:

```bash
cd /opt/trading-agents
git status --short
systemctl list-timers --all 'trading-*'
systemctl cat trading-premarket.service trading-premarket.timer
systemctl is-enabled trading-after-open.timer
systemctl is-active trading-after-open.timer trading-after-open.service
```

Review duplicate legacy schedules before staging. Do not start
app.services.trading_runner: it manages exits. Retain .env credentials and all
runtime reports, state and signals. Existing historical NinjaTrader signal files
are not deleted or invalidated by this deployment.

With all three new timer/service pairs disabled and idle (or not installed):

```bash
bash deploy/install.sh
systemctl cat trading-after-open.service trading-state.service trading-mnq.service
systemd-analyze calendar 'Mon..Fri *-*-* 09:45:00 America/New_York'
```

The staging script checks dependencies, runs the isolated tests, verifies paper
and dry-run configuration, validates units, copies them and reloads systemd. It
does not enable timers or run a reporting job. Review the existing sudo policy;
it uses noninteractive sudo and does not grant new permissions. The existing
premarket service/timer is not replaced.

## Verify active schedules

The manual `Verify reporting schedules` GitHub workflow checks installed unit
identity, effective safety commands, timer enablement and current timer activity
through SSM without running reports. Three new timers are enabled/active:

- After-open: weekdays at 09:45 Eastern (06:45 Pacific).
- State monitoring: every two minutes, with reporting gated to the NYSE session.
- MNQ proxy monitoring: every two minutes, offset by one minute, with the same
  session gate. Out-of-session timer ticks log a skip.

Activation was verified in Actions run 39074752345. Earlier unit files are backed
up under `/var/backups/trading-agents-reporting/20261017T222250Z`. The one-time
activation trigger was removed after success. No reporting service was manually
started and no live delivery test was performed. The next regular session is
Monday, October 19, 2026.

Before future unit upgrades, disable the three reporting timers, wait for their
services to be idle, stage and verify the new units, then explicitly enable the
three timers. Preserve the independent premarket and intraday schedules. Do not
start app.services.trading_runner or bypass a unit-verification failure.

Telegram delivery is at least once: a crash after sending but before saving state
can duplicate an alert. MNQ coverage is RTH only, with an NQ proxy. Automated
failure notifications and automatic rollback are not included.

## Recovery

Revert a reviewed code change on main and deploy the revert through OIDC/SSM;
do not reset or clean the EC2 checkout. Preserve .env, runtime state and reports.
If timers are enabled in a future activation phase, stop/disable the three new
timers before recovery and verify their services are idle. Preserve the separate
premarket schedule. No broker orders or exits are part of recovery.
