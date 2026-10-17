import os
from typing import Any, Dict

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://data.alpaca.markets/v1beta1/options"
HEADERS = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY"),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY"),
}


def get_snapshot(symbol: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/snapshots/{symbol}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return {}
    return r.json() or {}


def enrich_contract(contract: dict) -> dict:
    """
    Enrich one option contract dict with Alpaca IV/Greeks/quote fields.
    Safe no-throw behavior: returns original contract if snapshot is missing.
    """
    symbol = contract.get("symbol")
    if not symbol:
        return contract

    snap = get_snapshot(symbol)

    greeks = snap.get("greeks") or {}
    quote = snap.get("latest_quote") or {}

    contract["iv"] = snap.get("implied_volatility")
    contract["delta"] = greeks.get("delta")
    contract["gamma"] = greeks.get("gamma")
    contract["theta"] = greeks.get("theta")
    contract["vega"] = greeks.get("vega")

    contract["bid"] = quote.get("bid_price")
    contract["ask"] = quote.get("ask_price")
    contract["bid_size"] = quote.get("bid_size")
    contract["ask_size"] = quote.get("ask_size")

    return contract