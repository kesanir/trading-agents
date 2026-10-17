"""
features/risk_evaluator.py  (extended)
Pure evaluation — no I/O. Handles equity, option, CreditSpread, IronCondor, CalendarSpread.
"""
from __future__ import annotations

from core.config import settings
from core.market_hours import is_options_expiry_cutoff
from core.models import (
    AssetClass, CalendarSpread, CloseReason, CreditSpread, IronCondor,
    MultiLegStructure, Position, PositionSide, RiskAssessment, StructureType,
)
from core.logging import get_logger

logger = get_logger(__name__)


def _profit_pct(position: Position) -> float:
    plpc = position.unrealized_plpc
    return -plpc if position.side == PositionSide.SHORT else plpc


def _check_expiry_risk(expiries: list[str]) -> bool:
    return any(is_options_expiry_cutoff(exp) for exp in expiries)


def evaluate_single_position(position: Position) -> RiskAssessment:
    symbol = position.symbol
    stype = StructureType.SINGLE_OPTION if position.asset_class == AssetClass.OPTION else StructureType.EQUITY
    near_expiry = False

    if position.option_meta:
        near_expiry = _check_expiry_risk([position.option_meta.expiry])
        if near_expiry and position.side == PositionSide.SHORT:
            return RiskAssessment(
                symbol=symbol, structure_type=stype, should_close=True,
                reason=CloseReason.EXPIRY_RISK,
                details=f"Short option at/past expiry cutoff — closing to avoid assignment",
                orphan_position=position, near_expiry=True,
            )

    profit_pct = _profit_pct(position)

    if profit_pct <= -settings.MAX_LOSS_PCT:
        return RiskAssessment(
            symbol=symbol, structure_type=stype, should_close=True,
            reason=CloseReason.STOP_LOSS,
            details=f"Loss {profit_pct:.1%} >= threshold -{settings.MAX_LOSS_PCT:.0%}",
            orphan_position=position, near_expiry=near_expiry,
        )
    if profit_pct >= settings.PROFIT_TARGET_PCT:
        return RiskAssessment(
            symbol=symbol, structure_type=stype, should_close=True,
            reason=CloseReason.TAKE_PROFIT,
            details=f"Profit {profit_pct:.1%} >= target {settings.PROFIT_TARGET_PCT:.0%}",
            orphan_position=position, near_expiry=near_expiry,
        )
    return RiskAssessment(
        symbol=symbol, structure_type=stype, should_close=False, reason=None,
        details=f"Within thresholds: P&L {profit_pct:.1%}",
        orphan_position=position, near_expiry=near_expiry,
    )


def evaluate_credit_spread(spread: CreditSpread) -> RiskAssessment:
    underlying = spread.underlying
    stype = spread.structure_type
    expiries = [spread.short_leg.position.option_meta.expiry, spread.long_leg.position.option_meta.expiry]
    near_expiry = _check_expiry_risk(expiries)

    if near_expiry:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.EXPIRY_RISK,
            details=f"{stype.value} near expiry — closing to prevent assignment",
            structure=spread, near_expiry=True,
        )
    if spread.net_credit <= 0:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=False, reason=None,
            details="Net credit <= 0; skipping", structure=spread,
        )

    plpc = spread.unrealized_plpc
    if plpc <= -settings.SPREAD_LOSS_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.SPREAD_STOP_LOSS,
            details=f"Spread loss {plpc:.1%} >= -{settings.SPREAD_LOSS_PCT:.0%} | credit={spread.net_credit:.2f} cost={spread.current_value:.2f}",
            structure=spread,
        )
    if plpc >= settings.SPREAD_PROFIT_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.SPREAD_TAKE_PROFIT,
            details=f"Spread profit {plpc:.1%} >= {settings.SPREAD_PROFIT_PCT:.0%} | credit={spread.net_credit:.2f} cost={spread.current_value:.2f}",
            structure=spread,
        )
    return RiskAssessment(
        symbol=underlying, structure_type=stype, should_close=False, reason=None,
        details=f"Spread within thresholds: {plpc:.1%}", structure=spread,
    )


def evaluate_iron_condor(condor: IronCondor) -> RiskAssessment:
    underlying = condor.underlying
    stype = StructureType.IRON_CONDOR
    expiries = list({
        condor.put_spread.short_leg.position.option_meta.expiry,
        condor.call_spread.short_leg.position.option_meta.expiry,
    })
    near_expiry = _check_expiry_risk(expiries)

    if near_expiry:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.EXPIRY_RISK,
            details="Iron condor near expiry — closing all 4 legs",
            structure=condor, near_expiry=True,
        )
    if condor.net_credit <= 0:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=False, reason=None,
            details="Condor net credit <= 0", structure=condor,
        )

    plpc = condor.unrealized_plpc
    if plpc <= -settings.SPREAD_LOSS_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.CONDOR_STOP_LOSS,
            details=f"Condor loss {plpc:.1%} >= -{settings.SPREAD_LOSS_PCT:.0%}",
            structure=condor,
        )
    if plpc >= settings.SPREAD_PROFIT_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.CONDOR_TAKE_PROFIT,
            details=f"Condor profit {plpc:.1%} >= {settings.SPREAD_PROFIT_PCT:.0%}",
            structure=condor,
        )
    return RiskAssessment(
        symbol=underlying, structure_type=stype, should_close=False, reason=None,
        details=f"Condor within thresholds: {plpc:.1%}", structure=condor,
    )


def evaluate_calendar(cal: CalendarSpread) -> RiskAssessment:
    underlying = cal.underlying
    stype = StructureType.CALENDAR
    near_expiry = _check_expiry_risk([cal.short_leg.position.option_meta.expiry])

    if near_expiry:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.EXPIRY_RISK,
            details="Calendar near-leg expiring — close to avoid naked long",
            structure=cal, near_expiry=True,
        )
    if cal.net_debit <= 0:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=False, reason=None,
            details="Net debit <= 0", structure=cal,
        )

    plpc = cal.unrealized_plpc
    if plpc <= -settings.MAX_LOSS_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.STOP_LOSS,
            details=f"Calendar loss {plpc:.1%} >= -{settings.MAX_LOSS_PCT:.0%}",
            structure=cal,
        )
    if plpc >= settings.PROFIT_TARGET_PCT:
        return RiskAssessment(
            symbol=underlying, structure_type=stype, should_close=True,
            reason=CloseReason.TAKE_PROFIT,
            details=f"Calendar profit {plpc:.1%} >= {settings.PROFIT_TARGET_PCT:.0%}",
            structure=cal,
        )
    return RiskAssessment(
        symbol=underlying, structure_type=stype, should_close=False, reason=None,
        details=f"Calendar within thresholds: {plpc:.1%}", structure=cal,
    )


def evaluate_structure(structure: MultiLegStructure) -> RiskAssessment:
    if isinstance(structure, IronCondor):
        return evaluate_iron_condor(structure)
    if isinstance(structure, CreditSpread):
        return evaluate_credit_spread(structure)
    if isinstance(structure, CalendarSpread):
        return evaluate_calendar(structure)
    raise TypeError(f"Unknown structure: {type(structure)}")
