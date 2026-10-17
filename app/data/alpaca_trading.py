"""
data/alpaca_client.py
Alpaca REST + broker API client. All external HTTP I/O is isolated here.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, ClosePositionRequest, LimitOrderRequest, OptionLegRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, PositionIntent
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)


def get_trading_client() -> TradingClient:
    return TradingClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )


async def fetch_all_positions(client: TradingClient) -> list[dict[str, Any]]:
    """Return all open positions as raw dicts."""
    try:
        positions = client.get_all_positions()
        return [p.model_dump() for p in positions]
    except Exception as exc:
        logger.error("fetch_all_positions failed: %s", exc)
        raise


async def fetch_account(client: TradingClient) -> dict[str, Any]:
    try:
        account = client.get_account()
        return account.model_dump()
    except Exception as exc:
        logger.error("fetch_account failed: %s", exc)
        raise


async def fetch_open_orders(client: TradingClient) -> list[dict[str, Any]]:
    """Return all open/pending orders."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = client.get_orders(filter=req)
        return [o.model_dump() for o in orders]
    except Exception as exc:
        logger.error("fetch_open_orders failed: %s", exc)
        raise


async def close_equity_position(
    client: TradingClient,
    symbol: str,
    qty: str | None = None,
    percentage: str | None = None,
) -> dict[str, Any]:
    """Close an equity position fully or partially."""
    try:
        req = ClosePositionRequest(qty=qty, percentage=percentage)
        order = client.close_position(symbol_or_asset_id=symbol, close_options=req)
        logger.info("Closed equity position %s: order_id=%s", symbol, order.id)
        return order.model_dump()
    except Exception as exc:
        logger.error("close_equity_position %s failed: %s", symbol, exc)
        raise


async def submit_option_order(
    client: TradingClient,
    symbol: str,
    qty: int,
    side: OrderSide,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> dict[str, Any]:
    """Submit a single-leg option order (used to close individual legs)."""
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        order = client.submit_order(order_data=req)
        logger.info(
            "Option order submitted: %s %s x%s -> order_id=%s",
            side, symbol, qty, order.id
        )
        return order.model_dump()
    except Exception as exc:
        logger.error("submit_option_order %s failed: %s", symbol, exc)
        raise


async def cancel_order(client: TradingClient, order_id: str) -> None:
    try:
        client.cancel_order_by_id(order_id)
        logger.info("Cancelled order %s", order_id)
    except Exception as exc:
        logger.warning("cancel_order %s failed: %s", order_id, exc)


# ── Options data ─────────────────────────────────────────────────────────────

def get_option_data_client() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )


async def fetch_option_chain(
    underlying: str,
    expiration_gte: date | None = None,
    expiration_lte: date | None = None,
    contract_type: str | None = None,
) -> dict[str, Any]:
    """Fetch option chain snapshots with greeks for an underlying symbol.

    Returns dict mapping OCC symbol -> snapshot with greeks.
    """
    try:
        client = get_option_data_client()
        from alpaca.trading.enums import ContractType
        req = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=expiration_gte,
            expiration_date_lte=expiration_lte,
            type=ContractType(contract_type) if contract_type else None,
        )
        chain = client.get_option_chain(req)
        logger.info(
            "Fetched option chain for %s: %d contracts (exp %s to %s)",
            underlying, len(chain), expiration_gte, expiration_lte,
        )
        return chain
    except Exception as exc:
        logger.error("fetch_option_chain %s failed: %s", underlying, exc)
        raise


# ── Multi-leg order submission ───────────────────────────────────────────────

async def submit_spread_order(
    client: TradingClient,
    short_symbol: str,
    long_symbol: str,
    qty: int,
    limit_price: float,
    is_credit: bool,
) -> dict[str, Any]:
    """Submit a two-leg spread as an MLEG limit order.

    For credit spreads: short_symbol is sold-to-open, long_symbol is bought-to-open.
    For debit spreads: long_symbol is bought-to-open, short_symbol is sold-to-open.
    limit_price: always positive — net credit received or net debit paid.
    """
    try:
        legs = [
            OptionLegRequest(
                symbol=short_symbol,
                ratio_qty=1,
                side=OrderSide.SELL,
                position_intent=PositionIntent.SELL_TO_OPEN,
            ),
            OptionLegRequest(
                symbol=long_symbol,
                ratio_qty=1,
                side=OrderSide.BUY,
                position_intent=PositionIntent.BUY_TO_OPEN,
            ),
        ]
        req = LimitOrderRequest(
            qty=qty,
            side=OrderSide.SELL if is_credit else OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.MLEG,
            limit_price=limit_price,
            legs=legs,
        )
        order = client.submit_order(order_data=req)
        logger.info(
            "Spread order submitted: %s/%s x%d limit=$%.2f %s -> order_id=%s",
            short_symbol, long_symbol, qty, limit_price,
            "CREDIT" if is_credit else "DEBIT", order.id,
        )
        return order.model_dump()
    except Exception as exc:
        logger.error("submit_spread_order failed: %s", exc)
        raise
