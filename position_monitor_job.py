"""Paper/dry-run EC2 entry point for the migrated Windows position monitor."""
import asyncio
import logging

from app.services.deployment_safety import check_safety
from app.services.session_calendar import in_session


def main():
    logging.basicConfig(level=logging.INFO)
    check_safety()
    if not in_session():
        logging.info('Position monitor skipped: outside NYSE session, holiday or early close.')
        return
    from app.services.trading_runner import run_once
    report = asyncio.run(run_once())
    if report.errors:
        raise RuntimeError('Position monitor failed; inspect the service journal for details.')


if __name__ == '__main__':
    main()
