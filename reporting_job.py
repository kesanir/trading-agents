"""Only these reporting entry points are used by the EC2 units."""
import argparse
import logging
import subprocess
import sys
from app.services.deployment_safety import check_safety
from app.services.session_calendar import in_session

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=["after-open", "state", "mnq", "gamma-prepare", "gamma-publish"])
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    check_safety()
    gamma_job = args.job.startswith('gamma-')
    if not (in_session(premarket=True) if gamma_job else in_session()):
        logging.info("Skipped: outside US equity session, holiday, or early close")
        return
    if gamma_job:
        from app.services.gamma_report_job import run
        run(args.job, args.telegram)
        return
    if args.job == "mnq":
        from mnq_monitor import run
        run(args.telegram)
        return
    script = "daily_runner.py" if args.job == "after-open" else "state_change_runner.py"
    command = [sys.executable, script, "--dry-run"]
    if args.job == "after-open":
        command.append("--open")
    if args.telegram:
        command.append("--telegram")
    subprocess.run(command, check=True, timeout=840)

if __name__ == "__main__":
    main()
