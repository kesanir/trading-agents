from __future__ import annotations

from typing import List, Dict

from alpaca.trading.client import TradingClient
from core.config import settings

from app.features.position_parser import parse_all_positions, detect_structures


def evaluate_exit_conditions() -> List[Dict]:
    client = TradingClient(
        settings.ALPACA_API_KEY,
        settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )

    raw_positions = [p.__dict__ for p in client.get_all_positions()]

    positions = parse_all_positions(raw_positions)
    structures, _ = detect_structures(positions)

    exits = []

    for s in structures:

        # -----------------------------
        # CREDIT SPREADS / CONDORS
        # -----------------------------
        if hasattr(s, "net_credit"):

            total_pl = sum(
                leg.position.unrealized_pl for leg in s.all_legs()
            )

            max_loss = getattr(s, "max_loss", None)

            if not max_loss or max_loss == 0:
                continue

            pnl_pct = total_pl / max_loss

            # -----------------------------
            # EXIT RULES (CORRECT NOW)
            # -----------------------------

            if pnl_pct >= 0.5:
                exits.append({
                    "underlying": s.underlying,
                    "type": type(s).__name__,
                    "reason": "TAKE_PROFIT",
                    "pnl_pct": round(pnl_pct, 2),
                })

            elif pnl_pct <= -1.0:
                exits.append({
                    "underlying": s.underlying,
                    "type": type(s).__name__,
                    "reason": "STOP_LOSS",
                    "pnl_pct": round(pnl_pct, 2),
                })

    return exits