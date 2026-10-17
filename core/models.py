"""
core/models.py  (extended)
Pure dataclasses / value objects. No business logic.
Adds IronCondor, CalendarSpread, DiagonalSpread, and RunRecord for persistence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AssetClass(str, Enum):
    EQUITY = "us_equity"
    OPTION = "us_option"
    CRYPTO = "crypto"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


class OptionRight(str, Enum):
    CALL = "C"
    PUT = "P"


class CloseReason(str, Enum):
    STOP_LOSS              = "stop_loss"
    TAKE_PROFIT            = "take_profit"
    SPREAD_STOP_LOSS       = "spread_stop_loss"
    SPREAD_TAKE_PROFIT     = "spread_take_profit"
    CONDOR_STOP_LOSS       = "condor_stop_loss"
    CONDOR_TAKE_PROFIT     = "condor_take_profit"
    EXPIRY_RISK            = "expiry_risk"
    RISK_VALIDATION_FAILED = "risk_validation_failed"


class StructureType(str, Enum):
    EQUITY         = "equity"
    SINGLE_OPTION  = "single_option"
    BULL_PUT       = "bull_put"
    BEAR_CALL      = "bear_call"
    IRON_CONDOR    = "iron_condor"
    CALENDAR       = "calendar"
    DIAGONAL       = "diagonal"
    UNKNOWN_SPREAD = "unknown_spread"


@dataclass
class OptionMeta:
    underlying: str
    expiry: str          # YYYYMMDD
    strike: float
    right: OptionRight


@dataclass
class Position:
    symbol: str
    asset_class: AssetClass
    side: PositionSide
    qty: float
    avg_entry_price: float
    current_price: float
    market_value: float
    cost_basis: float
    unrealized_pl: float
    unrealized_plpc: float   # decimal e.g. -0.25 = -25%
    option_meta: Optional[OptionMeta] = None


@dataclass
class SpreadLeg:
    position: Position
    role: str  # "short" | "long" | "short_call" | etc.


@dataclass
class CreditSpread:
    underlying: str
    short_leg: SpreadLeg
    long_leg: SpreadLeg
    net_credit: float
    max_loss: float
    spread_type: str   # "bull_put" | "bear_call"

    @property
    def current_value(self) -> float:
        short_val = self.short_leg.position.current_price * abs(self.short_leg.position.qty) * 100
        long_val  = self.long_leg.position.current_price  * abs(self.long_leg.position.qty)  * 100
        return short_val - long_val

    @property
    def unrealized_pl(self) -> float:
        return self.net_credit - self.current_value

    @property
    def unrealized_plpc(self) -> float:
        return self.unrealized_pl / self.net_credit if self.net_credit else 0.0

    @property
    def structure_type(self) -> StructureType:
        return StructureType.BULL_PUT if self.spread_type == "bull_put" else StructureType.BEAR_CALL

    def all_legs(self) -> list[SpreadLeg]:
        return [self.short_leg, self.long_leg]


@dataclass
class IronCondor:
    underlying: str
    put_spread: CreditSpread
    call_spread: CreditSpread

    @property
    def net_credit(self) -> float:
        return self.put_spread.net_credit + self.call_spread.net_credit

    @property
    def max_loss(self) -> float:
        return max(self.put_spread.max_loss, self.call_spread.max_loss) - self.net_credit

    @property
    def current_value(self) -> float:
        return self.put_spread.current_value + self.call_spread.current_value

    @property
    def unrealized_pl(self) -> float:
        return self.net_credit - self.current_value

    @property
    def unrealized_plpc(self) -> float:
        return self.unrealized_pl / self.net_credit if self.net_credit else 0.0

    @property
    def structure_type(self) -> StructureType:
        return StructureType.IRON_CONDOR

    def all_legs(self) -> list[SpreadLeg]:
        return self.put_spread.all_legs() + self.call_spread.all_legs()


@dataclass
class CalendarSpread:
    underlying: str
    short_leg: SpreadLeg   # near-dated
    long_leg: SpreadLeg    # far-dated
    spread_type: str       # "call_calendar" | "put_calendar"

    @property
    def net_debit(self) -> float:
        return (
            self.long_leg.position.avg_entry_price
            - self.short_leg.position.avg_entry_price
        ) * abs(self.long_leg.position.qty) * 100

    @property
    def current_value(self) -> float:
        long_val  = self.long_leg.position.current_price  * abs(self.long_leg.position.qty)  * 100
        short_val = self.short_leg.position.current_price * abs(self.short_leg.position.qty) * 100
        return long_val - short_val

    @property
    def unrealized_pl(self) -> float:
        return self.current_value - self.net_debit

    @property
    def unrealized_plpc(self) -> float:
        return self.unrealized_pl / self.net_debit if self.net_debit else 0.0

    @property
    def structure_type(self) -> StructureType:
        return StructureType.CALENDAR

    def all_legs(self) -> list[SpreadLeg]:
        return [self.short_leg, self.long_leg]


MultiLegStructure = CreditSpread | IronCondor | CalendarSpread


class SpreadSide(str, Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class SpreadDirection(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass
class SpreadSetup:
    """Fully resolved spread ready for order submission."""
    underlying: str
    spread_side: SpreadSide       # credit or debit
    direction: SpreadDirection    # call or put
    expiration: str               # YYYYMMDD
    short_strike: float
    long_strike: float
    short_symbol: str             # OCC option symbol
    long_symbol: str
    short_delta: float
    long_delta: float
    net_price: float              # estimated net credit (positive) or debit (positive)
    qty: int

    @property
    def label(self) -> str:
        return f"{self.spread_side.value}_{self.direction.value}"

    @property
    def description(self) -> str:
        return (
            f"{self.underlying} {self.spread_side.value} {self.direction.value} spread "
            f"{self.short_strike}/{self.long_strike} exp {self.expiration} "
            f"x{self.qty} @ ${self.net_price:.2f}"
        )


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str]
    spread_setup: Optional[SpreadSetup]
    error: Optional[str] = None
    dry_run: bool = False


@dataclass
class RiskAssessment:
    symbol: str
    structure_type: StructureType
    should_close: bool
    reason: Optional[CloseReason]
    details: str
    structure: Optional[MultiLegStructure] = None
    orphan_position: Optional[Position] = None
    near_expiry: bool = False


@dataclass
class CloseAction:
    symbol: str
    structure_type: str
    reason: str
    legs_closed: list[str]
    order_ids: list[str]
    dry_run: bool
    error: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class RunRecord:
    run_id: str
    timestamp: datetime
    session: str
    account_equity: float
    account_buying_power: float
    total_positions: int
    structures_found: dict
    assessments_count: int
    closes_triggered: int
    errors: list[str]
    actions: list[dict]
