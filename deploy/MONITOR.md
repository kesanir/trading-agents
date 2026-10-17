# Windows position monitor migration

Windows task: `trading_agents_monitor` (run_monitor.bat launches
`python -m app.services.trading_runner`). EC2 equivalent:
`trading-agents-monitor.timer` → `trading-agents-monitor.service`.

Schedule: weekdays from 06:30 through 12:55 America/Los_Angeles, every five
minutes. Pacific daylight-saving changes are automatic. The entry point uses
the NYSE calendar to skip holidays and stop at early closes. There is no catch-up
run after downtime. A shared reporting/deployment lock prevents overlap.

The EC2 service forces ALPACA_PAPER=True, DRY_RUN=True and
SCAN_OUTSIDE_MARKET_HOURS=False. It reads account/position data and records risk
assessments under app/reports/monitor; it does not cancel orders, close positions,
or submit orders. Tests exercise the actual runner with mocked clients and
verify both cancellation and close paths are unreachable in dry run.

The EC2 timer is installed and activated only after the code deployment and
Linux checks pass. Existing reporting schedules are preserved. The installed-unit
verifier includes this service/timer pair once installed and rejects changes or
overrides that differ from the reviewed source.

At cutover, disable `trading_agents_monitor` in Task Scheduler on the original
Windows machine. That task was not found on the computer attached to this chat;
its Windows state could not be changed here. No local Windows task was disabled.

Verification: `systemctl is-enabled trading-agents-monitor.timer`,
`systemctl is-active trading-agents-monitor.timer`,
`systemctl list-timers --all trading-agents-monitor.timer`, and
`journalctl -u trading-agents-monitor.service --since today`.
The unit is oneshot: its service is normally inactive between scheduled scans.
