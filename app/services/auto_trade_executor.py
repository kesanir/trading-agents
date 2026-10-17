from __future__ import annotations

from typing import Dict, Any

from core.models import SpreadDirection, SpreadSide
from app.services.trade_placer import place_spread_trade
from app.services.risk_engine import compute_position_size


async def execute_trade(signal: Dict[str, Any], account_equity: float):
    ticker = str(signal["ticker"]).upper()
    direction = str(signal["direction"]).lower()
    strategy = str(signal.get("strategy", "credit_put")).lower()
    dte = int(signal.get("dte", 7))
    delta = float(signal.get("delta", 0.35))
    width = float(signal.get("width", 5))

    sizing = compute_position_size(account_equity, signal)
    qty = sizing["contracts"]

    if strategy == "credit_put":
        spread_side = SpreadSide.CREDIT
        spread_dir = SpreadDirection.PUT
    elif strategy == "credit_call":
        spread_side = SpreadSide.CREDIT
        spread_dir = SpreadDirection.CALL
    elif strategy == "debit_call":
        spread_side = SpreadSide.DEBIT
        spread_dir = SpreadDirection.CALL
    elif strategy == "debit_put":
        spread_side = SpreadSide.DEBIT
        spread_dir = SpreadDirection.PUT
    else:
        raise ValueError(f"Unsupported strategy: {strategy}")

    result = await place_spread_trade(
        underlying=ticker,
        spread_side=spread_side,
        direction=spread_dir,
        dte=dte,
        delta=delta,
        width=width,
        qty=qty,
    )

    return result, sizing