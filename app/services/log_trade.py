from app.services.trade_logger import log_trade


def main():
    trade = {
        "symbol": "MNQ",
        "bias": "LONG",
        "setup": "buy_dips",
        "day_type": "trend",
        "trigger_type": "opening_range_breakout_long",
        "entry": 23254,
        "exit": 23320,
        "pnl": 66,
        "result": "WIN",
    }

    log_trade(trade)
    print("Trade logged.")


if __name__ == "__main__":
    main()