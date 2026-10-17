from typing import TypedDict, Optional, Dict, Any

class TradingState(TypedDict):
    timestamp: str
    market_bias: Optional[str]
    regime: Optional[str]
    chop_score: float
    gex_regime: Optional[str]
    mag7_signal: Optional[Dict[str, Any]]
    futures_signal: Optional[Dict[str, Any]]
    risk_ok: bool
    alert_message: Optional[str]