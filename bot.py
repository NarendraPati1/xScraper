"""
bot.py
======
Telegram bot that responds to /update with a fresh AI news digest.

Commands:
    /start        — welcome message
    /update       — scrape and send top 5 AI tweets
    /more         — get next 5 tweets
    /liked        — show currently followed topics
    /clear_liked  — clear all saved preferences

Features:
    - Every tweet has an inline "Like ❤️" button.
    - Liking a tweet saves the topic to user_preferences.json using Gemini.
    - Future digests prioritize updates/follow-ups on liked topics (flagged with 🔄).

Run locally:
    python bot.py
"""

import asyncio
from datetime import datetime, timezone, timedelta

# IST timezone constant
_IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(_IST)
import html
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    LinkPreviewOptions,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from google import genai
from google.genai import types

# ── local modules ──────────────────────────────────────────
from scraper import main as run_scraper, scrape_custom_query
from rank_and_notify import (
    rank_with_gemini,
    rank_search_with_gemini,
    format_message,
    format_search_message,
    format_header,
    summarize_tweets,
    extract_topic_with_gemini,
    add_liked_tweet,
    add_disliked_tweet,
    remove_liked_topic,
    curate_feed_locally,
    load_preferences,
    save_preferences,
    add_shown_tweets,
    add_alert_topic,
    remove_alert_topic,
)

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
# TWEET CACHE  (persisted for Like button lookups)
# ─────────────────────────────────────────────────────────────
TWEETS_CACHE_FILE = Path("all_tweets_cache.json")


def save_tweets_cache(tweets: list[dict]) -> None:
    """Persist scraped tweets to disk so Like callbacks can look them up."""
    try:
        existing: dict[str, dict] = {}
        if TWEETS_CACHE_FILE.exists():
            with open(TWEETS_CACHE_FILE, "r", encoding="utf-8") as f:
                existing = {str(t["id"]): t for t in json.load(f)}
        for t in tweets:
            existing[str(t["id"])] = t
        with open(TWEETS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(existing.values()), f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Could not save tweets cache: {e}")


def get_tweet_from_cache(tweet_id: str) -> dict | None:
    """Retrieve a specific tweet from the on-disk cache by its ID."""
    if not TWEETS_CACHE_FILE.exists():
        return None
    try:
        with open(TWEETS_CACHE_FILE, "r", encoding="utf-8") as f:
            tweets = json.load(f)
        for t in tweets:
            if str(t.get("id")) == str(tweet_id):
                return t
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────
# RESULT CACHE & BACKGROUND SCHEDULER
# ─────────────────────────────────────────────────────────────
CACHE_MINUTES  = 30
# _user_caches will store: chat_id -> { 'results': top, 'more_pool': more_pool, 'at': datetime }
_user_caches   = {}
MORE_PAGE_SIZE = 5        # tweets per "More" press

# Global caches (loaded from scraper periodically in the background)
_global_scraped_tweets  = []
_global_ranked_results  = []  # list of (tweet, summary) — top 50, Gemini ranked globally
_global_more_summaries  = {}  # tweet_id -> summary for full pool (pre-computed for /more)
_global_scraped_at      = None
_cold_start_lock        = asyncio.Lock()  # prevents duplicate on-demand cold-start scrapes

RANKED_CACHE_FILE = Path("all_tweets_ranked_cache.json")


def save_ranked_cache(ranked_results: list[tuple[dict, str]]) -> None:
    """Save globally ranked results to disk cache."""
    try:
        serializable = [{"tweet": t, "summary": s} for t, s in ranked_results]
        with open(RANKED_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Could not save ranked cache: {e}")


def load_ranked_cache() -> list[tuple[dict, str]]:
    """Load globally ranked results from disk cache."""
    if RANKED_CACHE_FILE.exists():
        try:
            with open(RANKED_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [(item["tweet"], item["summary"]) for item in data]
        except Exception:
            pass
    return []


def load_tweets_cache() -> list[dict]:
    """Load cached tweets from disk cache."""
    if TWEETS_CACHE_FILE.exists():
        try:
            with open(TWEETS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


async def background_scraper_loop(application: Application) -> None:
    """Periodically scrapes X and pre-ranks + pre-summarizes the full pool with Gemini."""
    global _global_scraped_tweets, _global_ranked_results, _global_more_summaries, _global_scraped_at

    # 1. Warm up from disk
    _global_scraped_tweets = load_tweets_cache()
    _global_ranked_results = load_ranked_cache()
    if _global_ranked_results:
        _global_scraped_at = datetime.now()
        # Build more_summaries from ranked cache
        _global_more_summaries = {str(t["id"]): s for t, s in _global_ranked_results}
        log.info(f"Background Scraper: Warmed up — {len(_global_scraped_tweets)} raw, {len(_global_ranked_results)} ranked.")

    await asyncio.sleep(5)

    # Exponential backoff delays on failure: 2m, 5m, 15m, then 30m normal
    backoff_schedule = [2 * 60, 5 * 60, 15 * 60]
    failure_count = 0

    while True:
        log.info("Background Scraper: Starting scheduled scrape & global rank...")
        success = False
        try:
            tweets = await run_scraper(verbose=False)
            if tweets:
                _global_scraped_tweets = tweets
                _global_scraped_at = datetime.now()
                save_tweets_cache(tweets)

                # Rank top 50 globally (no user prefs — unbiased)
                ranked = await asyncio.to_thread(rank_with_gemini, tweets, chat_id=None, count=50)
                if ranked:
                    _global_ranked_results = ranked
                    save_ranked_cache(ranked)

                    # Pre-summarize ALL ranked tweets so /more is instant
                    log.info("Background Scraper: Pre-summarizing More pool...")
                    all_tweets_in_pool = [t for t, _ in ranked]
                    # Use existing summaries from ranking — Gemini already summarized them
                    _global_more_summaries = {str(t["id"]): s for t, s in ranked}
                    log.info(f"Background Scraper: Done — {len(ranked)} ranked, {len(_global_more_summaries)} summaries cached.")
                    success = True

                    # ── Direct User Alert Checks (Live Search & Alert) ──
                    from pathlib import Path
                    import re
                    log.info("Background Scraper: Starting direct alert notifications check...")
                    for pref_path in Path(".").glob("user_preferences_*.json"):
                        match = re.match(r"user_preferences_(\d+)\.json", pref_path.name)
                        if not match:
                            continue
                        user_chat_id = match.group(1)
                        try:
                            user_prefs = load_preferences(user_chat_id)
                            alert_topics = user_prefs.get("alert_topics", [])
                            if not alert_topics:
                                continue
                            
                            log.info(f"Checking alert subscriptions for user {user_chat_id} on topics: {alert_topics}")
                            for topic in alert_topics:
                                try:
                                    # Scrape X live for this specific topic
                                    scraped_tweets = await scrape_custom_query(topic, count=10, verbose=False)
                                    if not scraped_tweets:
                                        continue
                                    
                                    # Filter out already alerted tweets and keep only tweets posted in the last 35 minutes
                                    from email.utils import parsedate_to_datetime
                                    alerted_ids = set(user_prefs.get("alerted_tweets", []))
                                    now_utc = datetime.now(timezone.utc)
                                    
                                    def _is_alert_recent(tweet: dict) -> bool:
                                        try:
                                            created = parsedate_to_datetime(tweet.get("created", ""))
                                            # Keep only tweets created in the last 35 minutes (to align with 30-min background interval)
                                            return (now_utc - created).total_seconds() <= 2100
                                        except Exception:
                                            return False
                                            
                                    new_tweets = [
                                        t for t in scraped_tweets 
                                        if str(t["id"]) not in alerted_ids and _is_alert_recent(t)
                                    ]
                                    if not new_tweets:
                                        continue
                                    
                                    # Rank/summarize with Gemini
                                    matches = await asyncio.to_thread(rank_search_with_gemini, new_tweets, topic, 3)
                                    if not matches:
                                        continue
                                    
                                    # Send header
                                    await application.bot.send_message(
                                        chat_id=user_chat_id,
                                        text=f"🔔 <b>Direct Update: {html.escape(topic)}</b>",
                                        parse_mode="HTML"
                                    )
                                    
                                    # Save to cache so Likes buttons work
                                    save_tweets_cache([t for t, _ in matches])
                                    
                                    # Send tweets
                                    for rank, (tweet, summary) in enumerate(matches, 1):
                                        already_liked = _is_already_liked(tweet["id"], user_chat_id)
                                        already_disliked = _is_already_disliked(tweet["id"], user_chat_id)
                                        await application.bot.send_message(
                                            chat_id=user_chat_id,
                                            text=format_message(rank, tweet, summary),
                                            parse_mode="HTML",
                                            link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
                                            reply_markup=_action_buttons(tweet["id"], already_liked, already_disliked),
                                        )
                                        await asyncio.sleep(0.3)
                                    
                                    # Add to alerted list
                                    new_alerted_ids = list(alerted_ids) + [str(t["id"]) for t, _ in matches]
                                    if len(new_alerted_ids) > 100:
                                        new_alerted_ids = new_alerted_ids[-100:]
                                    user_prefs["alerted_tweets"] = new_alerted_ids
                                    save_preferences(user_prefs, user_chat_id)
                                    
                                except Exception as alert_exc:
                                    log.error(f"Error checking alert for '{topic}' / user {user_chat_id}: {alert_exc}")
                                
                                # Gentle sleep between topics to avoid rate limits
                                await asyncio.sleep(3)
                        except Exception as pref_exc:
                            log.error(f"Could not check alerts for user {user_chat_id}: {pref_exc}")
                else:
                    log.warning("Background Scraper: Gemini global ranking returned empty list.")
            else:
                log.warning("Background Scraper: No tweets returned from scraper.")
        except Exception as e:
            log.exception(f"Background Scraper: Error: {e}")

        if success:
            failure_count = 0
            await asyncio.sleep(30 * 60)
        else:
            # Exponential backoff
            delay = backoff_schedule[min(failure_count, len(backoff_schedule) - 1)]
            failure_count += 1
            log.info(f"Background Scraper: Retrying in {delay // 60} minutes (failure #{failure_count})...")
            await asyncio.sleep(delay)






# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _action_buttons(tweet_id: str, already_liked: bool = False, already_disliked: bool = False) -> InlineKeyboardMarkup:
    """Build inline keyboard with Like ❤️ and Dislike 👎 buttons."""
    if already_liked:
        like_btn = InlineKeyboardButton("Liked ❤️", callback_data=f"unlike_{tweet_id}")
    else:
        like_btn = InlineKeyboardButton("Like ❤️", callback_data=f"like_{tweet_id}")

    if already_disliked:
        dislike_btn = InlineKeyboardButton("Disliked 👎", callback_data=f"undislike_{tweet_id}")
    else:
        dislike_btn = InlineKeyboardButton("👎", callback_data=f"dislike_{tweet_id}")

    return InlineKeyboardMarkup([[like_btn, dislike_btn]])


# Keep backward-compat alias used in a few places
def _like_button(tweet_id: str, already_liked: bool = False, already_disliked: bool = False) -> InlineKeyboardMarkup:
    return _action_buttons(tweet_id, already_liked, already_disliked)


def _is_already_liked(tweet_id: str, chat_id: str) -> bool:
    prefs = load_preferences(chat_id)
    return str(tweet_id) in {str(t.get("id")) for t in prefs.get("liked_tweets", [])}


def _is_already_disliked(tweet_id: str, chat_id: str) -> bool:
    prefs = load_preferences(chat_id)
    return str(tweet_id) in {str(t.get("id")) for t in prefs.get("disliked_tweets", [])}


MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("Get AI Digest"), KeyboardButton("More")],
        [KeyboardButton("Update me about 🔔"), KeyboardButton("Active Alerts 📋")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Tap to manage digests and alerts",
)


# ─────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["more_offset"] = 0
    await update.message.reply_text(
        "<b>AI News Bot</b>\n\n"
        "Tap <b>Get AI Digest</b> to fetch the latest AI updates from X.\n"
        "Tap <b>More</b> to keep browsing more tweets.\n"
        "Tap <b>Like ❤️</b> on any tweet to track its topic — future digests will "
        "surface follow-up news on things you care about.\n"
        "Use /liked to see what you're following, or /clear_liked to reset.",
        parse_mode="HTML",
        reply_markup=MAIN_KEYBOARD,
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _global_scraped_tweets, _global_ranked_results, _global_more_summaries, _global_scraped_at

    chat_id = update.effective_chat.id
    context.user_data["more_offset"] = 0

    # ── Serve from user cache if still fresh ──────────────────
    now = datetime.now()
    user_cache = _user_caches.get(str(chat_id))
    if user_cache and not user_cache.get("dirty", False):
        age_mins = (now - user_cache["at"]).total_seconds() / 60
        if age_mins < CACHE_MINUTES and _global_scraped_at and user_cache["at"] >= _global_scraped_at:
            log.info(f"Serving cached results to chat_id={chat_id} (age: {age_mins:.1f} min)")
            await context.bot.send_message(chat_id=chat_id, text=format_header(), parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
            for rank, (tweet, summary) in enumerate(user_cache["results"], 1):
                already_liked = _is_already_liked(tweet["id"], str(chat_id))
                already_disliked = _is_already_disliked(tweet["id"], str(chat_id))
                await context.bot.send_message(
                    chat_id=chat_id, text=format_message(rank, tweet, summary), parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
                    reply_markup=_action_buttons(tweet["id"], already_liked, already_disliked),
                )
                await asyncio.sleep(0.3)
            return

    # ── Cold start: no global cache yet ──────────────────────
    if not _global_ranked_results:
        if _cold_start_lock.locked():
            await update.message.reply_text("Feed is initializing, please wait ~30 seconds and try again.")
            return
        async with _cold_start_lock:
            # Double-check after acquiring lock
            if not _global_ranked_results:
                init_msg = await update.message.reply_text("⏳ Initializing feed... (~60 seconds)\nI'll let you know when ready!")

                async def _progress_nudge():
                    await asyncio.sleep(20)
                    if not _global_ranked_results:
                        try:
                            await context.bot.send_message(chat_id=chat_id, text="Still working... almost there ⚙️")
                        except Exception:
                            pass
                asyncio.create_task(_progress_nudge())

                try:
                    tweets = await run_scraper(verbose=False)
                    if tweets:
                        _global_scraped_tweets = tweets
                        _global_scraped_at = datetime.now()
                        save_tweets_cache(tweets)
                        ranked = await asyncio.to_thread(rank_with_gemini, tweets, chat_id=None, count=50)
                        if ranked:
                            _global_ranked_results = ranked
                            _global_more_summaries = {str(t["id"]): s for t, s in ranked}
                            save_ranked_cache(ranked)
                    if not _global_ranked_results:
                        await update.message.reply_text("No tweets found on X. Please try again later.")
                        return
                except Exception as exc:
                    log.exception("Cold start scrape failed")
                    exc_str = str(exc)[:1000]
                    await context.bot.send_message(chat_id=chat_id, text=f"Failed to initialize: {html.escape(exc_str)}", parse_mode="HTML")
                    return

    # ── Instant local curation from global pre-ranked pool ────
    try:
        log.info(f"Curating locally for chat_id={chat_id}")
        top, more_pool = await curate_feed_locally(_global_ranked_results, str(chat_id))

        if not top:
            return

        _user_caches[str(chat_id)] = {"results": top, "more_pool": more_pool, "at": datetime.now()}
        log.info(f"User cache set for chat_id={chat_id}. Top={len(top)}, More={len(more_pool)}")

        await context.bot.send_message(chat_id=chat_id, text=format_header(), parse_mode="HTML", reply_markup=MAIN_KEYBOARD)
        for rank, (tweet, summary) in enumerate(top, 1):
            already_liked = _is_already_liked(tweet["id"], str(chat_id))
            already_disliked = _is_already_disliked(tweet["id"], str(chat_id))
            await context.bot.send_message(
                chat_id=chat_id, text=format_message(rank, tweet, summary), parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
                reply_markup=_action_buttons(tweet["id"], already_liked, already_disliked),
            )
            await asyncio.sleep(0.3)

    except Exception as exc:
        log.exception("Error during /update")
        exc_str = str(exc)[:1000]
        await context.bot.send_message(chat_id=chat_id, text=f"Something went wrong: {html.escape(exc_str)}", parse_mode="HTML")


async def cmd_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the next page of tweets from the More pool."""
    chat_id = update.effective_chat.id
    user_cache = _user_caches.get(str(chat_id))

    # If no user cache at all and no global data either — truly nothing loaded
    if not user_cache and not _global_ranked_results:
        await update.message.reply_text("No digest loaded yet — tap Get AI Digest first.")
        return

    # If user cache is missing or has no more_pool, fall back to global ranked results
    if not user_cache or not user_cache.get("more_pool"):
        if _global_ranked_results:
            # Build a synthetic more_pool from global results (skip first 10 as pseudo "top")
            results = user_cache.get("results", []) if user_cache else []
            shown_ids = {str(t["id"]) for t, _ in results}
            more_pool = [(t, s) for t, s in _global_ranked_results if str(t["id"]) not in shown_ids]
            if not more_pool:
                await update.message.reply_text("No additional tweets available right now.")
                return
        else:
            await update.message.reply_text("No additional tweets available right now.")
            return
    else:
        more_pool = user_cache["more_pool"]

    results = user_cache.get("results", []) if user_cache else []

    offset = context.user_data.get("more_offset", 0)
    batch  = more_pool[offset : offset + MORE_PAGE_SIZE]

    if not batch:
        await update.message.reply_text(
            f"You've seen all {len(more_pool) + len(results)} available tweets. "
            "Tap Get AI Digest to load a fresh batch."
        )
        return

    start_rank = len(results) + offset + 1

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>More from X</b>  —  {_now_ist().strftime('%d %b %Y, %I:%M %p IST')}",
        parse_mode="HTML",
    )

    for i, (tweet, summary) in enumerate(batch):
        # Use pre-computed summary from global cache; fallback to raw text
        cached_summary = _global_more_summaries.get(str(tweet["id"]), summary)
        already_liked = _is_already_liked(tweet["id"], str(chat_id))
        already_disliked = _is_already_disliked(tweet["id"], str(chat_id))
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_message(start_rank + i, tweet, cached_summary),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
            reply_markup=_action_buttons(tweet["id"], already_liked, already_disliked),
        )
        await asyncio.sleep(0.3)

    context.user_data["more_offset"] = offset + len(batch)
    add_shown_tweets([str(t["id"]) for t, _ in batch], str(chat_id))
    remaining = len(more_pool) - context.user_data["more_offset"]
    log.info(f"More batch sent (offset {offset}→{context.user_data['more_offset']}) to chat_id={chat_id}. {remaining} left.")


async def cmd_liked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's currently tracked topics as a ranked table."""
    chat_id = update.effective_chat.id
    prefs = load_preferences(str(chat_id))
    liked_tweets = prefs.get("liked_tweets", [])
    disliked_topics = prefs.get("disliked_topics", [])

    if not liked_tweets:
        await update.message.reply_text(
            "You haven't liked any posts yet.\n\n"
            "Tap <b>Like ❤️</b> on any news item to start tracking its topic.",
            parse_mode="HTML",
        )
        return

    # Build ranked table: topic -> {count, last_liked}
    from collections import defaultdict
    topic_stats: dict = defaultdict(lambda: {"count": 0, "last": ""})
    for lt in liked_tweets:
        t = lt.get("topic", "")
        if not t:
            continue
        topic_stats[t]["count"] += 1
        ts = lt.get("liked_at", "")
        if ts > topic_stats[t]["last"]:
            topic_stats[t]["last"] = ts

    # Sort by most recently liked
    sorted_topics = sorted(topic_stats.items(), key=lambda x: x[1]["last"], reverse=True)

    lines = ["<b>❤️ Your Tracked Topics</b>\n"]
    lines.append("<code>Topic                      Likes  Last Liked</code>")
    for topic, stats in sorted_topics:
        try:
            last_dt = datetime.fromisoformat(stats["last"]).strftime("%d %b")
        except Exception:
            last_dt = "—"
        count = stats["count"]
        topic_trunc = topic[:26].ljust(26)
        lines.append(f"<code>{topic_trunc} {count:^5}  {last_dt}</code>")

    if disliked_topics:
        lines.append(f"\n<i>Suppressed topics: {', '.join(disliked_topics)}</i>")

    lines.append("\n/unlike &lt;topic&gt; to remove a topic | /clear_liked to reset all")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_clear_liked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all user preferences."""
    chat_id = update.effective_chat.id
    save_preferences({
        "liked_topics": [],
        "liked_tweets": [],
        "disliked_topics": [],
        "disliked_tweets": [],
        "shown_tweets": []
    }, str(chat_id))
    await update.message.reply_text(
        "✅ All preferences cleared. Future digests will be unfiltered again.",
        parse_mode="HTML",
    )
    log.info(f"Preferences cleared by chat_id={chat_id}")


# ─────────────────────────────────────────────────────────────
# INLINE BUTTON CALLBACK
# ─────────────────────────────────────────────────────────────

async def callback_like(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tapping the 'Like ❤️' inline button on a tweet."""
    query = update.callback_query
    data = query.data or ""

    # noop button (already liked) — do nothing
    if data == "noop":
        await query.answer()
        return

    if not data.startswith("like_"):
        await query.answer()
        return

    tweet_id = data[len("like_"):]
    chat_id = str(query.message.chat_id)

    # Check if already liked
    if _is_already_liked(tweet_id, chat_id):
        await query.answer("Already tracking this topic! ❤️", show_alert=False)
        return

    # Look up the tweet from the on-disk cache
    tweet = get_tweet_from_cache(tweet_id)
    if not tweet:
        await query.answer("Could not find tweet data. Please try again after the next digest.", show_alert=True)
        return

    # Update the button to "Liked ❤️" instantly for visual feedback
    already_disliked = _is_already_disliked(tweet_id, chat_id)
    try:
        await query.edit_message_reply_markup(
            reply_markup=_like_button(tweet_id, already_liked=True, already_disliked=already_disliked)
        )
    except Exception as e:
        log.warning(f"Could not update reply markup instantly: {e}")

    # Extract topic using Gemini asynchronously (non-blocking)
    try:
        topic = await extract_topic_with_gemini(tweet)
    except Exception as e:
        log.error(f"Topic extraction failed: {e}")
        topic = " ".join((tweet.get("text", "AI News")).split()[:4])

    # Save to preferences and mark user cache as dirty (so next digest reflects interest, but pagination is kept)
    add_liked_tweet(tweet, topic, chat_id)
    if str(chat_id) in _user_caches:
        _user_caches[str(chat_id)]["dirty"] = True
    log.info(f"Liked tweet {tweet_id} by @{tweet.get('author')} — topic: '{topic}' for chat_id={chat_id}")

    await query.answer(f"Tracked: {topic} ❤️", show_alert=False)


async def callback_dislike(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tapping the 👎 dislike button on a tweet."""
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("dislike_"):
        await query.answer()
        return

    tweet_id = data[len("dislike_"):]
    chat_id = str(query.message.chat_id)

    tweet = get_tweet_from_cache(tweet_id)
    if not tweet:
        await query.answer("Could not find tweet data.", show_alert=True)
        return

    # 1. Answer and update markup IMMEDIATELY for zero-latency visual feedback
    await query.answer()
    already_liked = _is_already_liked(tweet_id, chat_id)
    try:
        await query.edit_message_reply_markup(
            reply_markup=_like_button(tweet_id, already_liked=already_liked, already_disliked=True)
        )
    except Exception:
        pass

    # 2. Extract topic in the background (non-blocking from user's perspective)
    try:
        topic = await extract_topic_with_gemini(tweet)
    except Exception:
        topic = " ".join(tweet.get("text", "").split()[:4])

    add_disliked_tweet(tweet, topic, chat_id)
    if str(chat_id) in _user_caches:
        _user_caches[str(chat_id)]["dirty"] = True
    log.info(f"Disliked topic '{topic}' for chat_id={chat_id}")

async def callback_unlike(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tapping 'Liked ❤️' again — removes the like."""
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("unlike_"):
        await query.answer()
        return

    tweet_id = data[len("unlike_"):]
    chat_id = str(query.message.chat_id)

    # Remove the liked tweet from preferences
    prefs = load_preferences(chat_id)
    prefs["liked_tweets"] = [t for t in prefs.get("liked_tweets", []) if str(t.get("id")) != tweet_id]
    # Rebuild liked_topics
    seen: set = set()
    prefs["liked_topics"] = []
    for t in reversed(prefs["liked_tweets"]):
        topic = t.get("topic", "")
        if topic and topic.lower() not in seen:
            seen.add(topic.lower())
            prefs["liked_topics"].append(topic)
    save_preferences(prefs, chat_id)

    if str(chat_id) in _user_caches:
        _user_caches[str(chat_id)]["dirty"] = True

    already_disliked = _is_already_disliked(tweet_id, chat_id)
    try:
        await query.edit_message_reply_markup(
            reply_markup=_action_buttons(tweet_id, already_liked=False, already_disliked=already_disliked)
        )
    except Exception:
        pass

    await query.answer("Like removed.", show_alert=False)
    log.info(f"Unliked tweet {tweet_id} for chat_id={chat_id}")


async def callback_undislike(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle tapping 'Disliked 👎' again — removes the dislike."""
    query = update.callback_query
    data = query.data or ""

    if not data.startswith("undislike_"):
        await query.answer()
        return

    tweet_id = data[len("undislike_"):]
    chat_id = str(query.message.chat_id)

    # Remove from disliked_tweets and rebuild disliked_topics
    prefs = load_preferences(chat_id)
    removed_tweet = next((t for t in prefs.get("disliked_tweets", []) if str(t.get("id")) == tweet_id), None)
    prefs["disliked_tweets"] = [t for t in prefs.get("disliked_tweets", []) if str(t.get("id")) != tweet_id]
    # Remove topic only if no other disliked tweet shares it
    if removed_tweet:
        topic = removed_tweet.get("topic", "").lower()
        remaining_topics = {t.get("topic", "").lower() for t in prefs["disliked_tweets"]}
        prefs["disliked_topics"] = [d for d in prefs.get("disliked_topics", []) if d.lower() in remaining_topics]
    save_preferences(prefs, chat_id)

    if str(chat_id) in _user_caches:
        _user_caches[str(chat_id)]["dirty"] = True

    already_liked = _is_already_liked(tweet_id, chat_id)
    try:
        await query.edit_message_reply_markup(
            reply_markup=_action_buttons(tweet_id, already_liked=already_liked, already_disliked=False)
        )
    except Exception:
        pass

    await query.answer("Dislike removed.", show_alert=False)
    log.info(f"Undisliked tweet {tweet_id} for chat_id={chat_id}")



async def cmd_unlike(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unlike <topic> — remove a specific tracked topic."""
    chat_id = update.effective_chat.id
    topic = " ".join(context.args or []).strip()

    if not topic:
        prefs = load_preferences(str(chat_id))
        topics = prefs.get("liked_topics", [])
        if topics:
            topic_list = "\n".join(f"  • {t}" for t in topics)
            await update.message.reply_text(
                f"Usage: /unlike &lt;topic&gt;\n\nYour topics:\n{topic_list}",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("You have no tracked topics to remove.")
        return

    removed = remove_liked_topic(topic, str(chat_id))
    if str(chat_id) in _user_caches:
        _user_caches[str(chat_id)]["dirty"] = True

    if removed:
        await update.message.reply_text(f"✅ Removed <b>{html.escape(topic)}</b> from your tracked topics.", parse_mode="HTML")
        log.info(f"Topic '{topic}' removed for chat_id={chat_id}")
    else:
        await update.message.reply_text(
            f"No topic matching \"<b>{html.escape(topic)}</b>\" was found.\n\nUse /liked to see your current topics.",
            parse_mode="HTML",
        )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <term> — live-scrape X/Twitter for the term, with fallback to local cached search."""
    chat_id = update.effective_chat.id
    term = " ".join(context.args or []).strip()

    if not term:
        await update.message.reply_text("Usage: /search &lt;topic&gt;\nExample: /search diffusion model", parse_mode="HTML")
        return

    live_success = False
    matches = []

    status_msg = await update.message.reply_text(
        f"🔍 Searching X/Twitter live for \"<b>{html.escape(term)}</b>\"...\nThis might take a few seconds.",
        parse_mode="HTML"
    )

    try:
        # Run custom search scrape on X
        tweets = await scrape_custom_query(term, count=15, verbose=False)
        # The X search succeeded, so we treat it as a live_success (no fallback to cached feed)
        live_success = True
        
        try:
            await status_msg.delete()
        except Exception:
            pass

        if tweets:
            # Save scraped tweets to the cache so liking them later works
            save_tweets_cache(tweets)
            # Rank and summarize with Gemini
            matches = await asyncio.to_thread(rank_search_with_gemini, tweets, term, 5)
    except Exception as exc:
        log.warning(f"Live search scraping failed: {exc}. Falling back to cached search.")
        try:
            await status_msg.edit_text(
                f"⚠️ Live search failed. Falling back to searching locally cached feed...",
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Fallback to local cached search if live search failed or returned nothing
    if not live_success:
        if not _global_ranked_results:
            await update.message.reply_text("No cached feed available to search. Tap <b>Get AI Digest</b> first.", parse_mode="HTML")
            return

        api_key = os.getenv("GEMINI_API_KEY")
        matches = []
        if api_key:
            try:
                # Build candidates list
                candidate_lines = []
                tweets_map = {}
                for t, s in _global_ranked_results:
                    tid = str(t["id"])
                    tweets_map[tid] = (t, s)
                    short = (" ".join(s.split()))[:200]
                    candidate_lines.append(f"ID:{tid} | {short}")
                
                candidates_block = "\n".join(candidate_lines)
                prompt = f"""You are a search engine for an AI news feed.
The user is searching for: "{term}"

Below are {len(_global_ranked_results)} pre-curated AI news tweets (ID | summary).
Find the top 5 tweets that are MOST semantically relevant to the user's search query.
Semantic relevance means conceptual similarity, matching the intent/topic of the search, not just exact keyword matches.

Return ONLY a JSON array of up to 5 matching objects, ordered from most to least relevant:
[{{"id": "<tweet_id>"}}]

Candidates:
{candidates_block}
"""
                client = genai.Client(api_key=api_key)
                response = await client.aio.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                import re as _re
                raw = (response.text or "").strip()
                raw = _re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
                matched_ids = json.loads(raw)
                
                seen = set()
                for item in matched_ids:
                    tid = str(item.get("id", "")).strip()
                    if tid in tweets_map and tid not in seen:
                        matches.append(tweets_map[tid])
                        seen.add(tid)
                    if len(matches) >= 5:
                        break
            except Exception as e:
                log.warning(f"Semantic search failed: {e}. Falling back to keyword search.")
                matches = []

        # Fallback to keyword search if semantic search found nothing / failed
        if not matches:
            term_lower = term.lower()
            words = [w for w in term_lower.split() if len(w) > 1]
            for tweet, summary in _global_ranked_results:
                content = (tweet.get("text", "") + " " + summary).lower()
                if any(w in content for w in words):
                    matches.append((tweet, summary))
                if len(matches) >= 5:
                    break

        try:
            await status_msg.delete()
        except Exception:
            pass

    if not matches:
        await update.message.reply_text(f"No results found for \"<b>{html.escape(term)}</b>\".", parse_mode="HTML")
        return

    header_text = f"<b>🔍 Search results for \"{html.escape(term)}\"</b>"
    if live_success:
        header_text += " (Live from X)"
    else:
        header_text += " (Cached Feed)"

    await context.bot.send_message(
        chat_id=chat_id,
        text=header_text,
        parse_mode="HTML",
    )
    for rank, (tweet, summary) in enumerate(matches, 1):
        already_liked = _is_already_liked(tweet["id"], str(chat_id))
        already_disliked = _is_already_disliked(tweet["id"], str(chat_id))
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_search_message(rank, tweet, summary),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(url=tweet["preview_url"]),
            reply_markup=_action_buttons(tweet["id"], already_liked, already_disliked),
        )
        await asyncio.sleep(0.3)


async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's active direct alerts."""
    chat_id = update.effective_chat.id
    prefs = load_preferences(str(chat_id))
    alert_topics = prefs.get("alert_topics", [])

    if not alert_topics:
        await update.message.reply_text(
            "You don't have any active alerts set up yet.\n\n"
            "Tap <b>Update me about 🔔</b> to create one.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )
        return

    lines = ["🔔 <b>Your Active Alerts</b>\n"]
    for idx, topic in enumerate(alert_topics, 1):
        lines.append(f"{idx}. <b>{html.escape(topic)}</b>")
    lines.append("\nUse `/unalert &lt;topic&gt;` to stop tracking a topic.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_KEYBOARD)


async def cmd_unalert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a topic from active alerts."""
    chat_id = update.effective_chat.id
    topic = " ".join(context.args or []).strip()

    if not topic:
        await update.message.reply_text("Usage: /unalert &lt;topic&gt;\nExample: /unalert Claude Code", parse_mode="HTML")
        return

    if remove_alert_topic(topic, str(chat_id)):
        await update.message.reply_text(
            f"✅ Stopped alerts for \"<b>{html.escape(topic)}</b>\".",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )
    else:
        await update.message.reply_text(
            f"No active alert found for \"<b>{html.escape(topic)}</b>\".\n\nUse /alerts to see your active list.",
            parse_mode="HTML"
        )


# ─────────────────────────────────────────────────────────────
# TEXT BUTTON HANDLER
# ─────────────────────────────────────────────────────────────

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    text_lower = text.lower()

    # Cancel alert topic waiting if they click a main button or run a command
    if text_lower in {"get ai digest", "more", "update me about 🔔", "active alerts 📋"} or text.startswith("/"):
        context.user_data["waiting_for_alert_topic"] = False

    if context.user_data.get("waiting_for_alert_topic"):
        context.user_data["waiting_for_alert_topic"] = False
        chat_id = update.effective_chat.id
        add_alert_topic(text, str(chat_id))
        await update.message.reply_text(
            f"🔔 Saved! You will now receive direct updates whenever anything related to \"<b>{html.escape(text)}</b>\" is posted on X.",
            parse_mode="HTML",
            reply_markup=MAIN_KEYBOARD
        )
        return

    if text_lower == "get ai digest":
        await cmd_update(update, context)
    elif text_lower == "more":
        await cmd_more(update, context)
    elif text_lower == "update me about 🔔":
        context.user_data["waiting_for_alert_topic"] = True
        await update.message.reply_text(
            "Please send the topic or keyword you want to receive direct alerts for (e.g. <code>Claude Code</code> or <code>Devin agent</code>):",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove()
        )
    elif text_lower == "active alerts 📋":
        await cmd_alerts(update, context)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

async def post_init(application: Application) -> None:
    """Start the background scraper after the bot initializes."""
    asyncio.ensure_future(background_scraper_loop(application))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suppress known non-critical errors (e.g. Conflict from another running instance)."""
    from telegram.error import Conflict
    if isinstance(context.error, Conflict):
        log.warning("Telegram Conflict: another bot instance is polling. Ensure only one instance of the bot is running.")
        return
    log.error(f"Unhandled exception: {context.error}", exc_info=context.error)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("update",      cmd_update))
    app.add_handler(CommandHandler("more",        cmd_more))
    app.add_handler(CommandHandler("liked",       cmd_liked))
    app.add_handler(CommandHandler("clear_liked", cmd_clear_liked))
    app.add_handler(CommandHandler("unlike",      cmd_unlike))
    app.add_handler(CommandHandler("search",      cmd_search))
    app.add_handler(CommandHandler("alerts",      cmd_alerts))
    app.add_handler(CommandHandler("unalert",     cmd_unalert))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(callback_like,       pattern=r"^like_"))
    app.add_handler(CallbackQueryHandler(callback_unlike,     pattern=r"^unlike_"))
    app.add_handler(CallbackQueryHandler(callback_dislike,    pattern=r"^dislike_"))
    app.add_handler(CallbackQueryHandler(callback_undislike,  pattern=r"^undislike_"))

    # Reply keyboard text buttons
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_button))

    # Error handler
    app.add_error_handler(error_handler)

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
