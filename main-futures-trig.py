#from app.data.market_data import get_intraday_context
#from app.agents.futures_trigger_agent import build_futures_trigger

#def main():
#    market = get_intraday_context("^NDX")

 #   result = build_futures_trigger(
#        current_price=market["price"],
 #       vwap=market["vwap"],
  ##      or_high=market["or_high"],
   #     or_low=market["or_low"],
  #      nq_price=market["price"],
   #     use_mag7=True,
   # )
#
  #  print("=== TRIGGER ===")
  #  print(result)

#if __name__ == "__main__":
 #   main()
 # 
from pprint import pprint

from app.data.alpaca_intraday import get_intraday_context
from app.agents.futures_trigger_agent import build_futures_trigger


def main():
    market = get_intraday_context("QQQ", opening_range_minutes=30)

    result = build_futures_trigger(
        current_price=market["price"],
        vwap=market["vwap"],
        or_high=market["or_high"],
        or_low=market["or_low"],
        nq_price=23254,   # replace with your current NQ/MNQ reference if you have it
        use_mag7=True,
    )

    print("=== FUTURES TRIGGER ===")
    pprint(result)


if __name__ == "__main__":
    main()