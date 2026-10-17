"""MNQ analysis using explicitly labelled NQ data; equity RTH only."""
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

STATE = Path("app/state/deployment_mnq.json")

def run(telegram=False):
    from app.services.deployment_safety import check_safety
    from app.services.session_calendar import in_session
    check_safety()
    if not in_session():
        return
    from app.data.futures_ticks import get_nq_intraday_context
    ctx = get_nq_intraday_context(opening_range_minutes=15)
    if not ctx:
        raise RuntimeError("MNQ analysis unavailable: native NQ feed unavailable; no QQQ substitution")
    last = ctx["bars"].index[-1].to_pydatetime()
    if last.tzinfo is None:
        raise RuntimeError("NQ bar has no timezone")
    age = datetime.now(timezone.utc) - last
    if age > timedelta(minutes=5) or age < timedelta(seconds=-30):
        raise RuntimeError("NQ feed stale or timestamp in future")
    price, vwap = float(ctx["price"]), float(ctx["vwap"])
    state = {"vwap_side": "above" if price >= vwap else "below",
             "range": "above" if price > ctx["or_high"] else "below" if price < ctx["or_low"] else "inside"}
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    logging.info("MNQ analysis (NQ.c.0 proxy) price=%s state=%s bar=%s", price, state, last)
    if old.get("state") == state:
        # A return to the confirmed state breaks a pending transition sequence.
        if old.get("pending") is not None:
            temp = STATE.with_suffix(".tmp")
            temp.write_text(json.dumps({"state": state}), encoding="utf-8")
            temp.replace(STATE)
        return
    count = old.get("count", 0) + 1 if old.get("pending") == state else 1
    if count >= 2:
        message = f"MNQ analysis | NQ.c.0 proxy | {last.isoformat()}\nPrice: {price}; VWAP: {vwap}\n{state}\nAnalysis only"
        if telegram:
            from app.services.telegram_service import send_telegram_message
            send_telegram_message(message, parse_mode=None)
        old = {"state": state}
    else:
        old.update(pending=state, count=count)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(old), encoding="utf-8")
    temp.replace(STATE)
