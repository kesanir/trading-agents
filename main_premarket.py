import sys
from app.agents.premarket_agent import build_premarket_plan
from app.services.formatting_service import format_premarket_plan
from app.services.telegram_service import send_telegram_message
from app.services.vix_apmem_service import build_vix_apmem_signal
from app.services.vix_apmem_report import format_vix_apmem_section


def main():
    send_telegram = "--telegram" in sys.argv

    plan = build_premarket_plan("QQQ")
    report = format_premarket_plan(plan)

    try:
        vix_apmem_sig = build_vix_apmem_signal()
        report += "\n\n" + format_vix_apmem_section(vix_apmem_sig)
    except Exception as e:
        report += f"\n\n=== VOLATILITY REGIME ===\nUnavailable: {e}"

    print(report)

    if send_telegram:
        send_telegram_message(report)


if __name__ == "__main__":
    main()
