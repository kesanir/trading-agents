"""
features/spread_builder.py
Selects option strikes by delta and DTE from the live option chain,
then assembles a SpreadSetup ready for order submission.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from core.config import settings
from core.logging import get_logger
from core.models import SpreadDirection, SpreadSetup, SpreadSide
from app.data.alpaca_trading import fetch_option_chain

logger = get_logger(__name__)

_OCC_RE = re.compile(
    r"^(?P<underlying>[A-Z]{1,6})"
    r"(?P<expiry>\d{6})"
    r"(?P<right>[CP])"
    r"(?P<strike>\d{8})$"
)


def _parse_occ(symbol: str) -> dict | None:
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    return {
        "underlying": m.group("underlying"),
        "expiry": "20" + m.group("expiry"),  # YYYYMMDD
        "right": m.group("right"),
        "strike": int(m.group("strike")) / 1000.0,
    }


def _expiry_to_date(expiry: str) -> date:
    """Convert YYYYMMDD string to date."""
    return date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))


def _pick_expiration(expirations: set[str], target_dte: int) -> str:
    """Find the expiration closest to target_dte from today."""
    today = date.today()
    best = None
    best_diff = float("inf")
    for exp in expirations:
        exp_date = _expiry_to_date(exp)
        dte = (exp_date - today).days
        if dte < 1:
            continue
        diff = abs(dte - target_dte)
        if diff < best_diff:
            best_diff = diff
            best = exp
    if best is None:
        raise ValueError(f"No valid expiration found near {target_dte} DTE")
    chosen_dte = (_expiry_to_date(best) - today).days
    logger.info("Selected expiration %s (%d DTE, target was %d)", best, chosen_dte, target_dte)
    return best


def _pick_strike_by_delta(
    contracts: list[dict],
    target_delta: float,
    is_put: bool,
) -> dict:
    """Find the contract closest to the target delta.

    For calls: delta is positive (0 to 1.0), target_delta is positive.
    For puts: delta is negative (-1.0 to 0), we compare abs(delta) to target_delta.
    """
    best = None
    best_diff = float("inf")
    for c in contracts:
        delta = c.get("delta")
        if delta is None:
            continue
        if is_put:
            diff = abs(abs(delta) - target_delta)
        else:
            diff = abs(delta - target_delta)
        if diff < best_diff:
            best_diff = diff
            best = c
    if best is None:
        raise ValueError(f"No contract found near delta {target_delta}")
    return best


def _find_protection_strike(
    contracts: list[dict],
    anchor_strike: float,
    width: float,
    is_credit: bool,
    is_put: bool,
) -> dict:
    """Find the closest available strike for the protection (long) leg.

    Credit call spread: long leg is ABOVE the short strike (buy higher call).
    Credit put spread:  long leg is BELOW the short strike (buy lower put).
    Debit call spread:  short leg is ABOVE the long strike (sell higher call).
    Debit put spread:   short leg is BELOW the long strike (sell lower put).
    """
    if is_credit:
        # Protection leg is further OTM
        if is_put:
            target_strike = anchor_strike - width  # buy lower put
        else:
            target_strike = anchor_strike + width  # buy higher call
    else:
        # Debit: anchor is the bought strike, protection is the sold strike
        if is_put:
            target_strike = anchor_strike - width  # sell lower put
        else:
            target_strike = anchor_strike + width  # sell higher call

    best = None
    best_diff = float("inf")
    for c in contracts:
        diff = abs(c["strike"] - target_strike)
        if diff < best_diff:
            best_diff = diff
            best = c
    if best is None:
        raise ValueError(f"No protection strike found near {target_strike}")
    return best


def _estimate_mid_price(snap: Any) -> float:
    """Estimate mid-price from snapshot's latest quote (bid/ask)."""
    quote = getattr(snap, "latest_quote", None)
    if quote is not None:
        bid = getattr(quote, "bid_price", 0) or 0
        ask = getattr(quote, "ask_price", 0) or 0
        if bid > 0 and ask > 0:
            return (bid + ask) / 2.0
    trade = getattr(snap, "latest_trade", None)
    if trade is not None:
        price = getattr(trade, "price", 0) or 0
        if price > 0:
            return price
    return 0.0


async def build_spread(
    underlying: str,
    spread_side: SpreadSide,
    direction: SpreadDirection,
    dte: int | None = None,
    delta: float | None = None,
    width: float | None = None,
    qty: int | None = None,
) -> SpreadSetup:
    """Fetch the option chain and select strikes to build a spread.

    Parameters use config defaults when not specified.
    """
    dte = dte or settings.SPREAD_DTE_TARGET
    width = width or settings.SPREAD_WIDTH
    qty = qty or settings.SPREAD_DEFAULT_QTY
    is_put = direction == SpreadDirection.PUT
    is_credit = spread_side == SpreadSide.CREDIT

    if delta is None:
        delta = settings.SPREAD_PUT_DELTA if is_put else settings.SPREAD_CALL_DELTA

    # Date window: target DTE ±15 days
    today = date.today()
    exp_gte = today + timedelta(days=max(1, dte - 15))
    exp_lte = today + timedelta(days=dte + 15)
    contract_type = "put" if is_put else "call"

    logger.info(
        "Building %s %s spread for %s: target DTE=%d delta=%.2f width=$%.1f qty=%d",
        spread_side.value, direction.value, underlying, dte, delta, width, qty,
    )

    # 1. Fetch chain
    chain = await fetch_option_chain(
        underlying=underlying,
        expiration_gte=exp_gte,
        expiration_lte=exp_lte,
        contract_type=contract_type,
    )
    if not chain:
        raise ValueError(f"Empty option chain for {underlying} ({contract_type}, {exp_gte} to {exp_lte})")

    # 2. Parse into workable list grouped by expiration
    contracts_by_exp: dict[str, list[dict]] = {}
    snapshots: dict[str, Any] = {}  # symbol -> snapshot
    for occ_symbol, snap in chain.items():
        parsed = _parse_occ(occ_symbol)
        if parsed is None:
            continue
        greeks = getattr(snap, "greeks", None)
        entry = {
            "symbol": occ_symbol,
            "strike": parsed["strike"],
            "expiry": parsed["expiry"],
            "right": parsed["right"],
            "delta": getattr(greeks, "delta", None) if greeks else None,
        }
        contracts_by_exp.setdefault(parsed["expiry"], []).append(entry)
        snapshots[occ_symbol] = snap

    if not contracts_by_exp:
        raise ValueError(f"No parseable contracts in chain for {underlying}")

    # 3. Pick expiration
    expiration = _pick_expiration(set(contracts_by_exp.keys()), dte)
    contracts = contracts_by_exp[expiration]

    # Filter contracts with valid delta
    contracts_with_delta = [c for c in contracts if c["delta"] is not None]
    if not contracts_with_delta:
        raise ValueError(f"No contracts with delta data for {underlying} exp {expiration}")

    # 4. Pick the anchor strike (the delta-targeted leg)
    anchor = _pick_strike_by_delta(contracts_with_delta, delta, is_put)

    # 5. Pick the protection strike
    protection = _find_protection_strike(contracts, anchor["strike"], width, is_credit, is_put)

    if anchor["symbol"] == protection["symbol"]:
        raise ValueError(
            f"Anchor and protection resolved to the same strike {anchor['strike']} — "
            f"try a wider spread width or different delta"
        )

    # 6. Determine short/long legs
    if is_credit:
        short_contract = anchor       # sell the delta-targeted strike
        long_contract = protection    # buy the further OTM strike
    else:
        long_contract = anchor        # buy the delta-targeted strike
        short_contract = protection   # sell the further OTM strike

    # 7. Estimate net price from mid-prices
    short_mid = _estimate_mid_price(snapshots.get(short_contract["symbol"]))
    long_mid = _estimate_mid_price(snapshots.get(long_contract["symbol"]))
    if is_credit:
        net_price = short_mid - long_mid  # credit received
    else:
        net_price = long_mid - short_mid  # debit paid
    net_price = max(0.01, round(net_price, 2))

    setup = SpreadSetup(
        underlying=underlying,
        spread_side=spread_side,
        direction=direction,
        expiration=expiration,
        short_strike=short_contract["strike"],
        long_strike=long_contract["strike"],
        short_symbol=short_contract["symbol"],
        long_symbol=long_contract["symbol"],
        short_delta=short_contract.get("delta") or 0.0,
        long_delta=long_contract.get("delta") or 0.0,
        net_price=net_price,
        qty=qty,
    )
    logger.info("Spread built: %s", setup.description)
    return setup
