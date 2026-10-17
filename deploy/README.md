# EC2 reporting deployment

Prepared against main commit 5526b667c7124bd2899b598b1373bad68a0ea527.
EC2 may have local safety fixes absent from main. Review its diff before updating;
never use reset --hard or git clean to force this deployment.

## Behavior

- After-open analysis: 09:45 America/New_York, weekdays, 15-minute opening range.
- State monitoring: every two minutes in the regular US equity session. Two-run
  confirmation and ten-minute cooldown; unchanged state stays quiet. The existing
  APMEM/GEX/trigger analysis is retained. Invalidation alerts are deduplicated.
- MNQ monitoring: every two minutes, explicitly using NQ.c.0 as a proxy. Requires
  Databento access and bars no more than five minutes old. No QQQ fallback.
  This deployment covers US equity regular hours only, not overnight CME sessions.
- NYSE holidays and early closes gate all new jobs. Calendar errors fail closed.
- All units force ALPACA_PAPER=True and DRY_RUN=True. Entry, exit and NinjaTrader
  file flags are separate, default false, and suppressed by dry run. The reporting
  deployment rejects non-paper or non-dry-run settings. Enabling execution needs
  a separately reviewed change and explicit user approval; it is not installed here.
- Existing premarket timer remains at 08:00 Eastern; its window now includes 08:00.
- Journald receives stdout/stderr. A shared lock serializes the three reporting
  jobs and deployments. Timeouts prevent stuck processes from running indefinitely.

## Before installing (run these in the Ubuntu SSH session)

```bash
cd /opt/trading-agents
git status --short
git diff -- daily_runner.py core/config.py app/services/market_hours_service.py
systemctl cat trading-premarket.service trading-premarket.timer
systemctl list-timers --all 'trading-*'
```

Commit/reconcile EC2-only code changes before updating. Review existing cron/systemd
jobs for duplicate runners. Do not start app.services.trading_runner: it manages exits.
Do not copy tracked historical signals/MNQ_signal.json into a NinjaTrader consumer.
This package does not delete or invalidate any preexisting NinjaTrader signal file.

Review and merge the deployment PR, then update the clean EC2 checkout:

```bash
git fetch origin main
git merge --ff-only origin/main
# Edit the existing .env without printing secrets:
nano .env
# Ensure these exact settings are present:
# ALPACA_PAPER=True
# DRY_RUN=True
# Retain Alpaca, Telegram and Databento credentials.
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s deployment_tests -v
.venv/bin/python -c 'from core.config import settings; assert settings.ALPACA_PAPER is True and settings.DRY_RUN is True; print("PASS: paper and dry run")'
bash deploy/install.sh
```

The installer uses sudo for unit installation and activation. GitHub SSH deployment
needs these sudo commands available without an interactive password; review the
instance's existing sudo policy rather than adding broad new sudo access.

## Verify

```bash
systemd-analyze calendar 'Mon..Fri *-*-* 09:45:00 America/New_York'
systemctl list-timers --all 'trading-*'
systemctl cat trading-after-open.service trading-state.service trading-mnq.service
# Safe outside session: each logs a skip, including weekends and holidays.
sudo systemctl start trading-after-open.service trading-state.service trading-mnq.service
journalctl -u trading-after-open -u trading-state -u trading-mnq --since today --no-pager
systemctl --failed
```

During a market session, verify one report, a confirmed changed-state Telegram
alert, silence on subsequent unchanged states, and an MNQ alert labelled NQ.c.0.
Missing/stale Databento data must fail the MNQ service visibly, without generating
signals. Failure status is visible through systemctl/journalctl; no separate
failure-notification channel is installed. Telegram delivery can duplicate after a
process crash between sending and saving state (at-least-once delivery).

The current tests cover safety controls and session rules; live data, Telegram,
Linux units, SSH and end-to-end GitHub deployment must be verified on EC2.

## GitHub Actions

The workflow tests PRs and deploys successful main commits. Create the GitHub
environment `ec2-reporting`; configure required reviewers if desired. Add secrets:

- EC2_HOST: instance public DNS/IP, reachable by the GitHub hosted runner.
- EC2_SSH_KEY: dedicated deployment private key (never commit it).
- EC2_KNOWN_HOSTS: trusted host-key line obtained/verified through your AWS console
  or an already trusted session. Do not disable StrictHostKeyChecking.

The instance needs read access to the repository. Bootstrap deploy/update.sh with
the initial installation above before running the workflow. A tracked dirty EC2
checkout stops deployment. Workflow deployments pass the tested commit SHA and
serialize with reporting jobs. New dependencies are installed into the existing
venv; this is not an atomic release or automatic rollback system. If installation
fails, inspect the Actions log and EC2 before resuming timers.

## Stop and recover

```bash
sudo systemctl disable --now trading-after-open.timer trading-state.timer trading-mnq.timer
sudo systemctl stop trading-after-open.service trading-state.service trading-mnq.service
```

Keep .env, app/state, reports, data and signals when recovering. Revert the relevant
deployment commit on main and deploy that reviewed revert; do not overwrite EC2
runtime files. The existing premarket timer is independent of these stop commands.

References: [exchange schedules](https://pandas-market-calendars.readthedocs.io/en/latest/usage.html),
[systemd timer options](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml).
