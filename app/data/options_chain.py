from app.data.polygon_client import get_options_chain  # your existing function

def fetch_options_chain(symbol: str):
    return get_options_chain(symbol)