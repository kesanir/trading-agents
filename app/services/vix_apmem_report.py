from __future__ import annotations

from app.services.vix_apmem_service import VixApmemSignal, build_vix_apmem_signal


def format_vix_apmem_section(sig: VixApmemSignal) -> str:
    lines = [
        "=== VOLATILITY REGIME ===",
        f"As of: {sig.as_of}",
        f"VIX: {sig.vix:.2f}",
        f"VIX Percentile: {sig.vix_percentile:.2f}",
        f"APMEM Vol: {sig.apmem_vol:.4f}",
        f"APMEM Trend: {sig.apmem_trend:.4f}",
        f"Score: {sig.score}/100",
        f"Regime: {sig.regime}",
        f"Trade Bias: {sig.bias}",
        f"SPY Trend: {sig.spy_trend:.4f}",
        f"QQQ Trend: {sig.qqq_trend:.4f}",
    ]

    if sig.score >= 70:
        lines.append("Interpretation: strong risk-on backdrop; favor long continuation setups.")
    elif sig.score >= 55:
        lines.append("Interpretation: moderate risk-on backdrop; favor selective longs.")
    elif sig.score <= 30:
        lines.append("Interpretation: strong risk-off backdrop; favor shorts, hedges, defensive posture.")
    elif sig.score <= 45:
        lines.append("Interpretation: moderate risk-off backdrop; lean short, reduce aggressive longs.")
    else:
        lines.append("Interpretation: mixed / transition regime; avoid oversized directional bets.")

    return "\n".join(lines)


def build_vix_apmem_section() -> str:
    return format_vix_apmem_section(build_vix_apmem_signal())
