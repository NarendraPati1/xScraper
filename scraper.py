"""
twikit_ai_news.py
=================
Fetch latest AI news tweets using twikit (no official API key needed).

Requirements:
    pip install twikit

Setup (one-time):
    1. Create a DEDICATED Twitter account (never use your personal one).
    2. Fill in your credentials in the CONFIG block below.
    3. Run once — cookies are saved to cookies.json automatically.
    4. On all future runs, cookies are reused (no re-login needed).
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
from twikit import Client
import twikit.x_client_transaction.transaction as tx_mod

load_dotenv()

# ── Cloudflare-bypass patch ───────────────────────────
# Render's IPs are blocked by Cloudflare on x.com, so the twikit
# ClientTransaction can never fetch the KEY_BYTE indices it needs.
# Fix: if TWITTER_TX_CACHE env var is set (JSON with pre-extracted
# values from a local machine), inject them directly into the
# transaction object at startup so x.com is never fetched.

_TX_CACHE: dict | None = None
_TX_CACHE_RAW = os.getenv("TWITTER_TX_CACHE", "")
if _TX_CACHE_RAW:
    try:
        _TX_CACHE = json.loads(_TX_CACHE_RAW)
        print("[+] Loaded TWITTER_TX_CACHE from environment.")
    except Exception as e:
        print(f"[!] Failed to parse TWITTER_TX_CACHE: {e}")


def _seed_transaction(ct: tx_mod.ClientTransaction) -> bool:
    """Pre-populate a ClientTransaction from the cached env values.
    Returns True if seeding succeeded, False otherwise."""
    if not _TX_CACHE:
        return False
    try:
        import bs4
        ct.DEFAULT_ROW_INDEX = _TX_CACHE["DEFAULT_ROW_INDEX"]
        ct.DEFAULT_KEY_BYTES_INDICES = _TX_CACHE["DEFAULT_KEY_BYTES_INDICES"]
        ct.key = _TX_CACHE["key"]
        ct.key_bytes = _TX_CACHE["key_bytes"]
        ct.animation_key = _TX_CACHE["animation_key"]
        # home_page_response must be truthy so twikit skips re-init;
        # use a minimal dummy BeautifulSoup object.
        ct.home_page_response = bs4.BeautifulSoup("<html></html>", "lxml")
        print("[+] Transaction pre-seeded from TWITTER_TX_CACHE.")
        return True
    except Exception as e:
        print(f"[!] Failed to seed transaction: {e}")
        return False

# ── End patch ────────────────────────────────────────

# ── Tweet property safety patch ──────────────────────
# twikit Tweet uses hard bracket access on legacy fields
# (e.g. legacy['favorite_count']) which raises KeyError when
# the Twitter API omits those fields, causing tweet_from_data
# to silently drop every result. Patch them to use .get().
from twikit.tweet import Tweet as _Tweet

_Tweet.favorite_count = property(lambda self: self._legacy.get('favorite_count', 0))
_Tweet.favorited      = property(lambda self: self._legacy.get('favorited', False))
_Tweet.reply_count    = property(lambda self: self._legacy.get('reply_count', 0))
_Tweet.retweet_count  = property(lambda self: self._legacy.get('retweet_count', 0))
# ── End tweet patch ──────────────────────────────────

# Reconfigure stdout to support unicode/emojis in Windows console
sys.stdout.reconfigure(encoding='utf-8')

# ─────────────────────────────────────────────
# CONFIG — fill these in before running
# ─────────────────────────────────────────────
TWITTER_USERNAME = os.getenv("TWITTER_USERNAME", "")
TWITTER_EMAIL    = os.getenv("TWITTER_EMAIL", "")
TWITTER_PASSWORD = os.getenv("TWITTER_PASSWORD", "")
COOKIES_FILE     = "cookies.json"

# Accounts to pull timeline tweets from directly
AI_ACCOUNTS = [
    "OpenAI",
    "AnthropicAI",
    "GoogleDeepMind",
    "mistralai",
    "huggingface",
    "karpathy",
    "ylecun",
    "sama",               # Sam Altman
]

# Search queries for latest AI news
SEARCH_QUERIES = [
    "AI model release lang:en min_faves:50",
    "LLM benchmark new lang:en min_faves:30",
    "new AI paper released lang:en",
    "GPT OR Claude OR Gemini OR Llama release",
]

# Minimum engagement to filter noise
# (Twitter's min_faves: operator in the query already handles filtering;
#  this is a safety floor — set to 0 to avoid dropping tweets where the
#  API doesn't return favorite_count in the response payload)
MIN_FAVORITES = 0
MAX_TWEETS_PER_QUERY = 10   # twikit max is 20 per call



# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def format_tweet(tweet) -> dict:
    """Extract the fields we care about from a Tweet object."""
    # Unwrap retweets so we get the original content
    source = tweet.retweeted_tweet if tweet.retweeted_tweet else tweet
    return {
        "id":        source.id,
        "author":    source.user.screen_name,
        "text":      source.text,
        "likes":     source.favorite_count or 0,
        "retweets":  source.retweet_count or 0,
        "replies":   source.reply_count or 0,
        "views":     source.view_count or 0,
        "created":   source.created_at,
        "url":       f"https://twitter.com/{source.user.screen_name}/status/{source.id}",
    }



def print_tweet(t: dict, index: int):
    print(f"\n{'─'*60}")
    print(f"[{index}] @{t['author']}  ·  {t['created']}")
    print(f"    {t['text'][:200]}{'...' if len(t['text']) > 200 else ''}")
    print(f"    ❤ {t['likes']}  🔁 {t['retweets']}  💬 {t['replies']}  👁 {t['views']}")
    print(f"    {t['url']}")


# ─────────────────────────────────────────────
# PERSISTENT CLIENT SINGLETON
# ─────────────────────────────────────────────
# We keep one Client instance alive for the entire process lifetime.
# twikit's ClientTransaction fetches x.com once on the first request
# to get KEY_BYTE indices. On cloud hosts, Cloudflare blocks repeated
# fetches. By reusing the same client, x.com is only fetched once
# at startup and the transaction is cached forever.

_client: Client | None = None


async def get_client() -> Client:
    """Return (and lazily initialize) the persistent twikit Client."""
    global _client
    if _client is not None:
        return _client

    client = Client("en-US")

    # ── Restore cookies ────────────────────────
    if not os.path.exists(COOKIES_FILE):
        env_cookies = os.getenv("TWITTER_COOKIES_JSON")
        if env_cookies:
            print("[+] Found TWITTER_COOKIES_JSON in environment. Restoring cookies...")
            try:
                with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                    f.write(env_cookies)
            except Exception as e:
                print(f"[!] Failed to write TWITTER_COOKIES_JSON to file: {e}")

    if os.path.exists(COOKIES_FILE):
        print(f"[+] Loading saved cookies from {COOKIES_FILE}")
        client.load_cookies(COOKIES_FILE)
    else:
        print("[+] No cookies found — logging in...")
        await client.login(
            auth_info_1=TWITTER_USERNAME,
            auth_info_2=TWITTER_EMAIL,
            password=TWITTER_PASSWORD,
        )
        client.save_cookies(COOKIES_FILE)
        print(f"[+] Cookies saved to {COOKIES_FILE}")

    # ── Pre-seed ClientTransaction so x.com is never fetched ──
    if not _seed_transaction(client.client_transaction):
        print("[!] TWITTER_TX_CACHE not set. twikit will attempt to fetch x.com on first request.")

    _client = client
    return _client


# ─────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────

async def main():
    client = await get_client()

    all_tweets = {}   # keyed by tweet ID to auto-deduplicate

    # ── 1. Search queries ──────────────────────
    print("\n[+] Running search queries...")
    for query in SEARCH_QUERIES:
        print(f"    Searching: {query}")
        try:
            results = await client.search_tweet(query, "Latest", count=MAX_TWEETS_PER_QUERY)
            for tweet in results:
                t = format_tweet(tweet)
                if t["likes"] >= MIN_FAVORITES and t["id"] not in all_tweets:
                    all_tweets[t["id"]] = t
        except Exception as e:
            print(f"    [!] Query failed: {e}")
        await asyncio.sleep(2)   # be polite between requests

    # ── 2. Account timelines ──────────────────
    print("\n[+] Fetching account timelines...")
    for username in AI_ACCOUNTS:
        print(f"    @{username}")
        try:
            user = await client.get_user_by_screen_name(username)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=5)
            for tweet in tweets:
                t = format_tweet(tweet)
                if t["id"] not in all_tweets:
                    all_tweets[t["id"]] = t
        except Exception as e:
            print(f"    [!] Failed for @{username}: {e}")
        await asyncio.sleep(1.5)

    # ── Results ───────────────────────────────
    tweets_list = sorted(all_tweets.values(), key=lambda x: x["likes"], reverse=True)

    print(f"\n{'='*60}")
    print(f"  Found {len(tweets_list)} unique AI tweets")
    print(f"{'='*60}")

    for i, t in enumerate(tweets_list[:20], 1):
        print_tweet(t, i)

    # ── Save to JSON ──────────────────────────
    output_file = f"ai_tweets_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(tweets_list, f, indent=2, ensure_ascii=False)
    print(f"\n[+] Full results saved to {output_file}")

    return tweets_list


if __name__ == "__main__":
    asyncio.run(main())