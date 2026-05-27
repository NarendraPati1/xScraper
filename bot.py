"""
bot.py
======
Telegram bot that responds to /update with a fresh AI news digest.

Commands:
    /start  — welcome message
    /update — scrape and send top 5 AI tweets
    /more   — get next 5 tweets

Run locally:
    python bot.py
"""

import asyncio
from datetime import datetime
import logging
import os
import sys

from dotenv import load_dotenv
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update, LinkPreviewOptions
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

# ─────────────────────────────────────────────────────────────
# CACHE  (30-minute in-memory result store)
# ─────────────────────────────────────────────────────────────
CACHE_MINUTES     = 30
MORE_PAGE_SIZE    = 5        # tweets per "More" press

_cache_lock       = asyncio.Lock()
_cached_results   = None    # list of (tweet, summary) — top 5, Gemini ranked
_cached_more_pool = None    # list of tweet dicts — everything else, sorted by likes
_cached_at        = None    # datetime of last successful scrape

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Get AI Digest"), KeyboardButton("More")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Tap to get today's AI digest",
)

# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["more_offset"] = 0
    await update.message.reply_text(
        "<b>AI News Bot</b>\n\n"
        "Tap <b>Get AI Digest</b> to fetch the latest AI updates from X.\n"
        "Tap <b>More</b> to keep browsing more tweets.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _cached_results, _cached_more_pool, _cached_at

    chat_id = update.effective_chat.id
    user    = update.effective_user.first_name or "there"

    # Reset this user's More pagination
    context.user_data["more_offset"] = 0

    # ── Serve from cache if fresh ──────────────────────────────
    now = datetime.now()
    if _cached_results and _cached_at:
        age_mins = (now - _cached_at).total_seconds() / 60
        if age_mins < CACHE_MINUTES:
            log.info(f"Serving cached results silently to chat_id={chat_id} (age: {age_mins:.1f} min)")
            await context.bot.send_message(chat_id=chat_id, text=format_header(), parse_mode="HTML")
            for rank, (tweet, summary) in enumerate(_cached_results, 1):
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=format_message(rank, tweet, summary),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
                )
                await asyncio.sleep(0.3)
            return

    # ── Cache stale/empty — scrape fresh ──────────────────────
    if _cache_lock.locked():
        await update.message.reply_text("Already scraping for another user — please wait a moment and try again.")
        return

    async with _cache_lock:
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

            # 2. Rank top 5 with AI
            log.info(f"Ranking {len(tweets)} tweets")
            top = rank_with_gemini(tweets)

            # 3. Build "More" pool — all remaining tweets not in top, sorted by likes
            top_ids = {t["id"] for t, _ in top}
            more_pool = [t for t in tweets if t["id"] not in top_ids]

            # 4. Store in cache
            _cached_results   = top
            _cached_more_pool = more_pool
            _cached_at        = datetime.now()
            log.info(f"Cache updated. Top={len(top)}, More pool={len(more_pool)} tweets available.")

            # 5. Send header + top 5
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
                    link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
                )
                await asyncio.sleep(0.3)

            log.info(f"Sent {len(top)} tweets to chat_id={chat_id}")

        except Exception as exc:
            log.exception("Error during /update")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Something went wrong: {exc}\nPlease try again in a moment."
            )


async def cmd_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the next page of tweets from the More pool."""
    chat_id = update.effective_chat.id

    if not _cached_more_pool:
        if not _cached_results:
            await update.message.reply_text("No digest loaded yet — tap Get AI Digest first.")
        else:
            await update.message.reply_text("No additional tweets available right now.")
        return

    # Get this user's current offset into the pool
    offset = context.user_data.get("more_offset", 0)
    batch  = _cached_more_pool[offset : offset + MORE_PAGE_SIZE]

    if not batch:
        await update.message.reply_text(
            f"You've seen all {len(_cached_more_pool) + len(_cached_results)} available tweets. "
            "Tap Get AI Digest to load a fresh batch."
        )
        return

    start_rank = len(_cached_results) + offset + 1  # e.g. 6, 11, 16 ...

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>More from X</b>  —  {datetime.now().strftime('%d %b %Y')}",
        parse_mode="HTML",
    )

    for i, tweet in enumerate(batch):
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_message(start_rank + i, tweet, tweet["text"]),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
        )
        await asyncio.sleep(0.3)

    # Advance this user's offset
    context.user_data["more_offset"] = offset + len(batch)
    remaining = len(_cached_more_pool) - context.user_data["more_offset"]
    log.info(f"Sent More batch (offset {offset}→{context.user_data['more_offset']}) to chat_id={chat_id}. {remaining} left.")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip().lower()
    if text == "get ai digest":
        await cmd_update(update, context)
    elif text == "more":
        await cmd_more(update, context)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("more",   cmd_more))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
