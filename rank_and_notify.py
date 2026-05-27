"""
rank_and_notify.py
==================
Runs the X scraper, selects the top 5 AI tweets by engagement,
and sends short clean messages to Telegram.

Setup:
    Fill in .env, then: python rank_and_notify.py
"""

import asyncio
import html
import json
import os
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = 5

# ─────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────

from scraper import main as run_scraper

# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def telegram_post(text: str, chat_id: str) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=data, timeout=10)
    if not resp.ok:
        print(f"  [!] Telegram error {resp.status_code}: {resp.text[:200]}")
    return resp.ok


def detect_chat_id() -> str | None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, timeout=10)
    if not resp.ok:
        return None
    for update in reversed(resp.json().get("result", [])):
        msg = update.get("message") or update.get("channel_post")
        if msg:
            return str(msg["chat"]["id"])
    return None


# ─────────────────────────────────────────────────────────────
# MESSAGE FORMATTER
# ─────────────────────────────────────────────────────────────

def format_message(rank: int, tweet: dict) -> str:
    author  = tweet["author"]
    url     = tweet["url"]
    body    = html.escape(tweet["text"])
    return f"<b>{rank}.</b>  @{author}\n{body}\n\n{url}"


def format_header() -> str:
    today = datetime.now().strftime("%d %b %Y")
    return f"<b>AI News Digest</b>  —  {today}"

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main():
    print("─" * 60)
    print("  AI Tweet Digest  |  Scrape > Notify")
    print("─" * 60)

    # 1. Scrape
    print("\n[1/2] Scraping tweets...")
    tweets = await run_scraper()
    if not tweets:
        print("  [!] No tweets returned. Exiting.")
        return
    print(f"  {len(tweets)} tweets collected.")

    # 2. Select top tweets by engagement (scraper already sorts them descending by likes)
    top = tweets[:TOP_N]
    print(f"  {len(top)} tweets selected.")

    # 3. Send
    print("\n[2/2] Sending to Telegram...")
    chat_id = TELEGRAM_CHAT_ID or detect_chat_id()
    if not chat_id:
        print("  [!] Could not detect chat ID. Send a message to your bot first.")
        return

    telegram_post(format_header(), chat_id)
    for rank, tweet in enumerate(top, 1):
        ok = telegram_post(format_message(rank, tweet), chat_id)
        print(f"  [{'ok' if ok else '!!'}] {rank}. @{tweet['author']}")

    print("\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
