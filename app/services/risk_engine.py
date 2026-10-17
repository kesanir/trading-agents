from __future__ import annotations

from typing import Dict, Any


def compute_position_size(
    account_equity: float,
    signal: Dict[str, Any],
    max_risk_pct: float = 0.005,   # 0.5% risk per trade
    max_portfolio_exposure: float = 0.20
) -> Dict[str, Any]:
    """
    Institutional-style position sizing
    """

    score = signal.get("score", 0)
    size_mult = signal.get("size_mult", 1)

    # -----------------------------
    # RISK BUDGET
    # -----------------------------
    risk_capital = account_equity * max_risk_pct

    # Confidence scaling
    confidence_factor = min(1.5, max(0.5, score * 2))

    adjusted_risk = risk_capital * confidence_factor * size_mult

    # -----------------------------
    # OPTIONS SPREAD ESTIMATION
    # -----------------------------
    # Assume $200 max loss per spread (5-wide credit)
    est_risk_per_contract = 200

    contracts = int(adjusted_risk / est_risk_per_contract)

    if contracts < 1:
        contracts = 1
    if contracts > 5:
        contracts = 5

    return {
        "contracts": contracts,
        "risk_capital": round(adjusted_risk, 2),
        "confidence_factor": round(confidence_factor, 2),
    }