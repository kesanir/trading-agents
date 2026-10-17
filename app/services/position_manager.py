from __future__ import annotations

from typing import List, Dict
from alpaca.trading.client import TradingClient
from core.config import settings


def get_open_positions() -> List[Dict]:
    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    positions = client.get_all_positions()

    return [
        {
            "symbol": p.symbol,
            "qty": p.qty,
            "unrealized_pl": float(p.unrealized_pl),
            "market_value": float(p.market_value),
        }
        for p in positions
    ]