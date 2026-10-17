from app.data.alpaca_intraday import get_intraday_context
from app.agents.futures_trigger_agent import build_futures_trigger
from app.services.formatting_service import format_futures_trigger


def main():
    market = get_intraday_context("QQQ", opening_range_minutes=30)

    trigger = build_futures_trigger(
        current_price=market["price"],
        vwap=market["vwap"],
        or_high=market["or_high"],
        or_low=market["or_low"],
        nq_price=23254,
        use_mag7=True,
    )

    report = format_futures_trigger(trigger)
    print(report)


if __name__ == "__main__":
    main()