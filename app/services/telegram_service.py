from __future__ import annotations

import os
import time
import logging
import requests
from typing import List

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("app.services.telegram")
if not log.handlers:
    logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
MAX_MESSAGE_LEN = 4000  # Telegram limit ~4096


# -----------------------------------------------------------------------------
# Core send
# -----------------------------------------------------------------------------

def _send(payload: dict) -> dict:
    r = requests.post(TELEGRAM_URL, json=payload, timeout=15)
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} | response={r.text}",
            response=r
        )
    return r.json()


# -----------------------------------------------------------------------------
# Message splitter (critical for your large reports)
# -----------------------------------------------------------------------------

def _split_message(text: str, limit: int = MAX_MESSAGE_LEN) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    while text:
        chunk = text[:limit]

        # try to split nicely at newline
        if len(text) > limit:
            split_idx = chunk.rfind("\n")
            if split_idx > 500:  # avoid tiny chunks
                chunk = chunk[:split_idx]

        chunks.append(chunk)
        text = text[len(chunk):]

    return chunks


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def send_telegram_message(
    message: str,
    parse_mode: str | None = None,
    disable_preview: bool = True,
    retries: int = 2,
) -> List[dict]:
    """
    Sends message to Telegram (auto-splits if too long)

    Returns list of responses (one per chunk)
    """

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    responses: List[dict] = []

    chunks = _split_message(message)

    for i, chunk in enumerate(chunks, start=1):
        payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": chunk,
        "disable_web_page_preview": disable_preview,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        attempt = 0
        while attempt <= retries:
            try:
                res = _send(payload)
                responses.append(res)
                break

            except Exception as e:
                attempt += 1
                log.warning(
                    "Telegram send failed (chunk %d/%d, attempt %d): %s",
                    i, len(chunks), attempt, e
                )
                time.sleep(1.5)

                if attempt > retries:
                    log.error("Failed to send Telegram message after retries")
                    raise

    log.info("Telegram sent (%d chunks)", len(chunks))
    return responses


# -----------------------------------------------------------------------------
# Convenience wrapper for premarket
# -----------------------------------------------------------------------------


def send_premarket_report(message: str) -> None:
    """
    Sends premarket report with header formatting
    """
    header = "📊 *PREMARKET REPORT*\n\n"
    send_telegram_message(header + message)


# -----------------------------------------------------------------------------
# Convenience wrapper for alerts
# -----------------------------------------------------------------------------

def send_alert(message: str) -> None:
    """
    Sends quick alert message
    """
    prefix = "🚨 *ALERT*\n\n"
    send_telegram_message(prefix + message)