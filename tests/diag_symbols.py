"""
diag_symbols.py — compare OI map symbols vs snapshot symbols for QQQ
Run: python diag_symbols.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, timedelta
from app.data.alpaca_client import _get, fetch_oi_map, TRADING_URL, OPTION_FEED

ticker = "QQQ"
today  = date.today()

# --- 5 symbols from OI map ---
oi_map = fetch_oi_map(ticker, min_dte=0, max_dte=45)
oi_syms = list(oi_map.keys())[:5]
print(f"\nOI map ({len(oi_map)} total) — sample symbols:")
for s in oi_syms:
    print(f"  {repr(s)}  oi={oi_map[s]}")

# --- 5 symbols from snapshot ---
d = _get(f"/v1beta1/options/snapshots/{ticker}", {
    "feed": OPTION_FEED,
    "limit": 5,
    "expiration_date_gte": today.isoformat(),
    "expiration_date_lte": (today + timedelta(days=45)).isoformat(),
})
snap_syms = list((d.get("snapshots") or {}).keys()) if d else []
print(f"\nSnapshot — sample symbols:")
for s in snap_syms:
    print(f"  {repr(s)}")

# --- Overlap check ---
snap_all = set()
url = f"/v1beta1/options/snapshots/{ticker}"
params = {
    "feed": OPTION_FEED, "limit": 1000,
    "expiration_date_gte": today.isoformat(),
    "expiration_date_lte": (today + timedelta(days=45)).isoformat(),
}
page = 0
while url and page < 3:  # just first 3 pages
    d = _get(url, params)
    if not d: break
    snap_all.update((d.get("snapshots") or {}).keys())
    nt = d.get("next_page_token")
    url = f"/v1beta1/options/snapshots/{ticker}" if nt else None
    params = {"feed": OPTION_FEED, "limit": 1000, "page_token": nt} if nt else {}
    page += 1

overlap = snap_all & set(oi_map.keys())
only_snap = list(snap_all - set(oi_map.keys()))[:3]
only_oi   = list(set(oi_map.keys()) - snap_all)[:3]

print(f"\nFirst 3 pages: {len(snap_all)} snapshot syms, {len(overlap)} overlap")
print(f"In snapshot but NOT in OI map: {only_snap}")
print(f"In OI map but NOT in snapshot: {only_oi}")
