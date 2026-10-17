from __future__ import annotations

from typing import Any, Dict, List

from alpaca.trading.client import TradingClient
from core.config import settings


def close_all_legs(structure) -> List[Dict[str, Any]]:
    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    results: List[Dict[str, Any]] = []

    for leg in structure.all_legs():
        symbol = leg.position.symbol
        try:
            res = client.close_position(symbol)
            results.append({
                "symbol": symbol,
                "status": "closed",
                "order_id": getattr(res, "id", None),
            })
        except Exception as e:
            results.append({
                "symbol": symbol,
                "status": "error",
                "error": str(e),
            })

    return results


def close_single_position(symbol: str) -> Dict[str, Any]:
    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    try:
        res = client.close_position(symbol)
        return {
            "symbol": symbol,
            "status": "closed",
            "order_id": getattr(res, "id", None),
        }
    except Exception as e:
        return {
            "symbol": symbol,
            "status": "error",
            "error": str(e),
        }