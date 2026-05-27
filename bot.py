"""
bot.py
======
Telegram bot that responds to /update with a fresh AI news digest.

Commands:
    /start  — welcome message
    /update — scrape, rank with Gemini, and send top 5 AI tweets
    /help   — show available commands

Run locally:
    python bot.py

Deploy (Railway / Render / Fly.io):
    See README or Dockerfile.
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
from rank_and_notify import rank_with_gemini, format_message, format_header

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "AI News Bot\n"
        "────────────────────\n"
        "Send /update to get the latest top 5 AI developments from X/Twitter,\n"
        "ranked and summarised by Gemini AI.\n\n"
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

    await update.message.reply_text(
        f"On it, {user}. Scraping X and ranking with Gemini — this takes about 60 seconds..."
    )

    try:
        # 1. Scrape
        log.info(f"Running scraper for chat_id={chat_id}")
        tweets = await run_scraper(verbose=False)

        if not tweets:
            await context.bot.send_message(chat_id=chat_id, text="No tweets found. Try again later.")
            return

        # 2. Rank + summarise
        log.info(f"Ranking {len(tweets)} tweets with Gemini")
        top = rank_with_gemini(tweets)

        # 3. Send header + individual messages
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_header(),
            parse_mode="HTML",
        )

        for rank, (tweet, summary) in enumerate(top, 1):
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_message(rank, tweet, summary),
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            await asyncio.sleep(0.3)   # avoid Telegram rate limits

        log.info(f"Sent {len(top)} tweets to chat_id={chat_id}")

    except Exception as exc:
        log.exception("Error during /update")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Something went wrong: {exc}\nPlease try again in a moment."
        )


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("update", cmd_update))

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
