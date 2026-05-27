"""
bot.py
======
Telegram bot that responds to /update with a fresh AI news digest.

Commands:
    /start  — welcome message
    /update — scrape and send top 5 AI tweets

Run locally:
    python bot.py
"""

import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Get AI Digest")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Tap to get today's AI digest",
)

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>AI News Bot</b>\n\n"
        "Tap <b>Get AI Digest</b> to fetch the latest AI updates from X.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user    = update.effective_user.first_name or "there"

    await update.message.reply_text(
        f"On it, {user}. Scraping X — this takes about 60 seconds..."
    )

    try:
        # 1. Scrape
        log.info(f"Running scraper for chat_id={chat_id}")
        tweets = await run_scraper(verbose=False)

        if not tweets:
            await context.bot.send_message(chat_id=chat_id, text="No tweets found. Try again later.")
            return

        # 2. Rank + summarise
        log.info(f"Ranking {len(tweets)} tweets")
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


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip().lower()
    if text == "get ai digest":
        await cmd_update(update, context)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
