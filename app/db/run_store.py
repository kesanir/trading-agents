"""
Minimal run-record store used by trading_runner.run_once().
Persists each monitor cycle as a JSON snapshot under app/reports/monitor/.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, is_dataclass

from app.services.persistence_service import save_report


async def init_db() -> None:
    return None


def make_run_id() -> str:
    return uuid.uuid4().hex[:12]


async def save_run_record(record) -> None:
    raw = asdict(record) if is_dataclass(record) else dict(record)
    save_report("monitor", raw, "")
