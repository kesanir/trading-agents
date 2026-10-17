"""
services/trade_placer.py
Orchestrates spread trade placement: build setup → submit order.
"""
from __future__ import annotations

from core.config import settings
from core.logging import get_logger
from core.models import SpreadDirection, SpreadSetup, SpreadSide, TradeResult
from app.data.alpaca_trading import get_trading_client, submit_spread_order
from app.features.spread_builder import build_spread

logger = get_logger(__name__)


async def place_spread_trade(
    underlying: str,
    spread_side: SpreadSide,
    direction: SpreadDirection,
    dte: int | None = None,
    delta: float | None = None,
    width: float | None = None,
    qty: int | None = None,
) -> TradeResult:
    """End-to-end: fetch chain → select strikes → submit MLEG order.

    Respects DRY_RUN — logs the trade but does not submit.
    """
    logger.info(
        "place_spread_trade: %s %s %s | dte=%s delta=%s width=%s qty=%s dry_run=%s",
        underlying, spread_side.value, direction.value,
        dte, delta, width, qty, settings.DRY_RUN,
    )

    # 1. Build the spread setup (chain fetch + strike selection)
    try:
        setup = await build_spread(
            underlying=underlying,
            spread_side=spread_side,
            direction=direction,
            dte=dte,
            delta=delta,
            width=width,
            qty=qty,
        )
    except Exception as exc:
        logger.error("Spread build failed for %s: %s", underlying, exc)
        return TradeResult(
            success=False, order_id=None, spread_setup=None,
            error=f"Spread build failed: {exc}",
        )

    # 2. Dry-run check
    if settings.DRY_RUN:
        logger.info("[DRY RUN] Would submit: %s", setup.description)
        return TradeResult(
            success=True, order_id="dry_run", spread_setup=setup, dry_run=True,
        )

    # 3. Submit the MLEG order
    try:
        client = get_trading_client()
        is_credit = spread_side == SpreadSide.CREDIT
        order = await submit_spread_order(
            client=client,
            short_symbol=setup.short_symbol,
            long_symbol=setup.long_symbol,
            qty=setup.qty,
            limit_price=setup.net_price,
            is_credit=is_credit,
        )
        order_id = str(order.get("id", "unknown"))
        logger.info("Trade placed: %s -> order_id=%s", setup.description, order_id)
        return TradeResult(
            success=True, order_id=order_id, spread_setup=setup,
        )
    except Exception as exc:
        logger.error("Order submission failed for %s: %s", setup.description, exc)
        return TradeResult(
            success=False, order_id=None, spread_setup=setup,
            error=f"Order submission failed: {exc}",
        )
