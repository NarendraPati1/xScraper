"""
bot.py
======
Telegram bot that responds to /update with a fresh AI news digest.

Commands:
    /start  — welcome message
    /update — scrape and send top 5 AI tweets
    /help   — show available commands

Run locally:
    python bot.py

Deploy (Render):
    Set RENDER_EXTERNAL_URL env var — bot auto-switches to webhook mode,
    which eliminates 409 Conflicts during rolling deploys.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── local modules ──────────────────────────────────────────
from scraper import main as run_scraper
from rank_and_notify import format_message, format_header

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

# Cache Configuration
CACHE_DURATION_SECS = 900  # 15 minutes
cache_lock = asyncio.Lock()
cached_top = None       # stores list of tweet dicts
cached_time = None      # stores datetime object of last successful scrape

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# Render sets RENDER_EXTERNAL_URL automatically (e.g. https://myapp.onrender.com).
# When present, we use webhook mode — Telegram pushes updates to our URL,
# no polling, no 409 Conflicts during rolling deploys.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", "10000"))

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "AI News Bot\n"
        "────────────────────\n"
        "Send /update to get the latest top 5 AI developments from X/Twitter.\n\n"
        "/help for all commands."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/update — fetch and rank the latest AI tweets\n"
        "/start  — introduction\n"
        "/help   — this message"
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user    = update.effective_user.first_name or "there"

    global cached_top, cached_time

    # Check cache first (without acquiring lock to avoid blocking check queries)
    now = datetime.now()
    if cached_top is not None and cached_time is not None:
        elapsed = (now - cached_time).total_seconds()
        if elapsed < CACHE_DURATION_SECS:
            log.info(f"Serving cached updates to chat_id={chat_id} (cache age: {elapsed:.1f}s)")
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_header(),
                parse_mode="HTML",
            )
            for rank, tweet in enumerate(cached_top, 1):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_message(rank, tweet),
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                await asyncio.sleep(0.3)
            return

    # If cache is stale/empty, acquire lock to perform scrape
    if cache_lock.locked():
        await update.message.reply_text(
            "Another update request is currently in progress. Please wait a moment while it finishes..."
        )
        
    async with cache_lock:
        # Re-check cache inside lock in case it was just populated by another concurrent task
        now = datetime.now()
        if cached_top is not None and cached_time is not None:
            elapsed = (now - cached_time).total_seconds()
            if elapsed < CACHE_DURATION_SECS:
                log.info(f"Serving newly populated cached updates to chat_id={chat_id} (cache age: {elapsed:.1f}s)")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_header(),
                    parse_mode="HTML",
                )
                for rank, tweet in enumerate(cached_top, 1):
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=format_message(rank, tweet),
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
                    await asyncio.sleep(0.3)
                return

        await update.message.reply_text(
            f"On it, {user}. Scraping X — this takes about 60 seconds..."
        )

        try:
            # 1. Scrape
            log.info(f"Running scraper for chat_id={chat_id}")
            tweets = await run_scraper()

            if not tweets:
                await context.bot.send_message(chat_id=chat_id, text="No tweets found. Try again later.")
                return

            # 2. Select top 5 (scraper returns them sorted by likes descending)
            log.info(f"Selecting top {min(len(tweets), 5)} tweets")
            top = tweets[:5]

            # Update Cache
            cached_top = top
            cached_time = datetime.now()
            log.info("Cache successfully updated with fresh scrape results.")

            # 3. Send header + individual messages
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_header(),
                parse_mode="HTML",
            )

            for rank, tweet in enumerate(top, 1):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_message(rank, tweet),
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
                await asyncio.sleep(0.3)   # avoid Telegram rate limits

            log.info(f"Sent {len(top)} fresh tweets to chat_id={chat_id}")

        except Exception as exc:
            log.exception("Error during /update")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Something went wrong: {exc}\nPlease try again in a moment."
            )


# ─────────────────────────────────────────────────────────────
# HEALTH CHECK SERVER (For Cloud Deployments)
# ─────────────────────────────────────────────────────────────

def start_health_server():
    port = int(os.getenv("PORT", "8000"))
    class HealthHandler(SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/health":
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            # Suppress standard logging to keep container outputs clean
            pass

    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"Starting health check web server on port {port}...")
    server.serve_forever()


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    # Start health check server in background thread for Render/Koyeb health checks
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("update", cmd_update))

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
