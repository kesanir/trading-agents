from __future__ import annotations

from typing import Any

from alpaca.trading.client import TradingClient
from core.config import settings


def has_open_position(symbol: str) -> bool:
    symbol = symbol.upper()

    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    try:
        positions = client.get_all_positions()
    except Exception:
        return False

    for pos in positions:
        pos_symbol = getattr(pos, "symbol", "")
        if str(pos_symbol).upper() == symbol:
            return True

    return False