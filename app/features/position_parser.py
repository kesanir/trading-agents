"""
features/position_parser.py  (extended)
Detects: CreditSpread, IronCondor, CalendarSpread.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from core.models import (
    AssetClass, CalendarSpread, CreditSpread, IronCondor,
    MultiLegStructure, OptionMeta, OptionRight,
    Position, PositionSide, SpreadLeg,
)
from core.logging import get_logger

logger = get_logger(__name__)

_OCC_RE = re.compile(
    r"^(?P<underlying>[A-Z]{1,6})"
    r"(?P<expiry>\d{6})"
    r"(?P<right>[CP])"
    r"(?P<strike>\d{8})$"
)


def _parse_option_meta(symbol: str) -> OptionMeta | None:
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    return OptionMeta(
        underlying=m.group("underlying"),
        expiry="20" + m.group("expiry"),
        strike=int(m.group("strike")) / 1000.0,
        right=OptionRight(m.group("right")),
    )


def parse_position(raw: dict[str, Any]) -> Position:
    symbol = raw["symbol"]
    try:
        asset_class = AssetClass(raw.get("asset_class", "us_equity"))
    except ValueError:
        asset_class = AssetClass.EQUITY
    qty = float(raw.get("qty", 0))
    side = PositionSide.LONG if qty > 0 else PositionSide.SHORT
    return Position(
        symbol=symbol, asset_class=asset_class, side=side, qty=qty,
        avg_entry_price=float(raw.get("avg_entry_price", 0)),
        current_price=float(raw.get("current_price", 0)),
        market_value=float(raw.get("market_value", 0)),
        cost_basis=float(raw.get("cost_basis", 0)),
        unrealized_pl=float(raw.get("unrealized_pl", 0)),
        unrealized_plpc=float(raw.get("unrealized_plpc", 0)),
        option_meta=_parse_option_meta(symbol) if asset_class == AssetClass.OPTION else None,
    )


def parse_all_positions(raw_list: list[dict[str, Any]]) -> list[Position]:
    out: list[Position] = []
    for raw in raw_list:
        try:
            out.append(parse_position(raw))
        except Exception as exc:
            logger.warning("Could not parse %s: %s", raw.get("symbol"), exc)
    return out


def detect_structures(
    positions: list[Position],
) -> tuple[list[MultiLegStructure], list[Position]]:
    options = [p for p in positions if p.asset_class == AssetClass.OPTION and p.option_meta]
    non_options = [p for p in positions if p.asset_class != AssetClass.OPTION]
    matched: set[str] = set()
    structures: list[MultiLegStructure] = []

    verticals = _find_verticals(options)
    condors, used_v = _pair_into_condors(verticals)
    for condor in condors:
        structures.append(condor)
        for leg in condor.all_legs():
            matched.add(leg.position.symbol)

    for spread in verticals:
        sl = spread.short_leg.position.symbol
        ll = spread.long_leg.position.symbol
        if sl in matched or ll in matched or id(spread) in used_v:
            continue
        structures.append(spread)
        matched.add(sl); matched.add(ll)

    remaining = [p for p in options if p.symbol not in matched]
    calendars, cal_map = _find_calendars(remaining)
    for cal in calendars:
        structures.append(cal)
        matched.update(cal_map[id(cal)])

    orphans = list(non_options) + [p for p in options if p.symbol not in matched]
    return structures, orphans


def _find_verticals(options: list[Position]) -> list[CreditSpread]:
    groups: dict[tuple, list[Position]] = defaultdict(list)
    for p in options:
        m = p.option_meta
        groups[(m.underlying, m.expiry, m.right.value)].append(p)

    spreads: list[CreditSpread] = []
    used: set[str] = set()

    for (underlying, expiry, right), legs in groups.items():
        shorts = [l for l in legs if l.side == PositionSide.SHORT and l.symbol not in used]
        longs  = [l for l in legs if l.side == PositionSide.LONG  and l.symbol not in used]
        for short in shorts:
            for long in longs:
                if long.symbol in used or short.symbol in used:
                    continue
                if abs(short.qty) != abs(long.qty):
                    continue
                stype = _classify_vertical(short, long, right)
                if stype is None:
                    continue
                net_credit = _estimate_net_credit(short, long)
                spread_width = abs(short.option_meta.strike - long.option_meta.strike)
                contracts = abs(short.qty)
                max_loss = max(0.0, spread_width - (net_credit / contracts / 100)) * contracts * 100
                spreads.append(CreditSpread(
                    underlying=underlying,
                    short_leg=SpreadLeg(position=short, role="short"),
                    long_leg=SpreadLeg(position=long, role="long"),
                    net_credit=net_credit, max_loss=max_loss, spread_type=stype,
                ))
                used.add(short.symbol); used.add(long.symbol)
                break
    return spreads


def _classify_vertical(short: Position, long: Position, right: str) -> str | None:
    ss, ls = short.option_meta.strike, long.option_meta.strike
    if right == "P" and ss > ls:
        return "bull_put"
    if right == "C" and ss < ls:
        return "bear_call"
    return None


def _estimate_net_credit(short: Position, long: Position) -> float:
    return (short.avg_entry_price - long.avg_entry_price) * abs(short.qty) * 100


def _pair_into_condors(
    verticals: list[CreditSpread],
) -> tuple[list[IronCondor], set]:
    bull_puts  = [v for v in verticals if v.spread_type == "bull_put"]
    bear_calls = [v for v in verticals if v.spread_type == "bear_call"]
    condors: list[IronCondor] = []
    used: set = set()

    for bp in bull_puts:
        for bc in bear_calls:
            if id(bp) in used or id(bc) in used:
                continue
            bp_exp = bp.short_leg.position.option_meta.expiry
            bc_exp = bc.short_leg.position.option_meta.expiry
            if bp.underlying != bc.underlying or bp_exp != bc_exp:
                continue
            if bp.short_leg.position.option_meta.strike >= bc.short_leg.position.option_meta.strike:
                continue
            condors.append(IronCondor(underlying=bp.underlying, put_spread=bp, call_spread=bc))
            used.add(id(bp)); used.add(id(bc))
            break
    return condors, used


def _find_calendars(
    options: list[Position],
) -> tuple[list[CalendarSpread], dict[int, set[str]]]:
    groups: dict[tuple, list[Position]] = defaultdict(list)
    for p in options:
        m = p.option_meta
        groups[(m.underlying, str(m.strike), m.right.value)].append(p)

    calendars: list[CalendarSpread] = []
    leg_map: dict[int, set[str]] = {}
    used: set[str] = set()

    for (underlying, strike, right), legs in groups.items():
        if len(legs) < 2:
            continue
        shorts = [l for l in legs if l.side == PositionSide.SHORT and l.symbol not in used]
        longs  = [l for l in legs if l.side == PositionSide.LONG  and l.symbol not in used]
        for short in shorts:
            for long in longs:
                if long.symbol in used or short.symbol in used:
                    continue
                if abs(short.qty) != abs(long.qty):
                    continue
                if short.option_meta.expiry == long.option_meta.expiry:
                    continue
                # Ensure short = near-dated
                near, far = (short, long) if short.option_meta.expiry < long.option_meta.expiry else (long, short)
                if near.side != PositionSide.SHORT:
                    continue
                stype = f"{'call' if right == 'C' else 'put'}_calendar"
                cal = CalendarSpread(
                    underlying=underlying,
                    short_leg=SpreadLeg(position=near, role="short"),
                    long_leg=SpreadLeg(position=far, role="long"),
                    spread_type=stype,
                )
                calendars.append(cal)
                leg_map[id(cal)] = {near.symbol, far.symbol}
                used.add(near.symbol); used.add(far.symbol)
                break
    return calendars, leg_map


# Backwards compat alias used by existing tests
def detect_credit_spreads(
    positions: list[Position],
) -> tuple[list[CreditSpread], list[Position]]:
    options = [p for p in positions if p.asset_class == AssetClass.OPTION and p.option_meta]
    non_options = [p for p in positions if p.asset_class != AssetClass.OPTION]
    verticals = _find_verticals(options)
    matched = {v.short_leg.position.symbol for v in verticals} | {v.long_leg.position.symbol for v in verticals}
    orphans = non_options + [p for p in options if p.symbol not in matched]
    return verticals, orphans
