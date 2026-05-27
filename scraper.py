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

# ── Monkey Patch ──────────────────────────────────────────────
# On cloud hosts (Render, Koyeb), Cloudflare blocks repeated GET
# requests to x.com used to initialize the ClientTransaction.
# We patch get_indices to SKIP the x.com fetch once the transaction
# is already initialized, reusing the cached home_page_response.
import twikit.x_client_transaction.transaction as tx_mod
original_get_indices = tx_mod.ClientTransaction.get_indices

async def patched_get_indices(self, home_page_response, session, headers):
    """If already initialized, skip re-fetching x.com for indices."""
    if (
        self.DEFAULT_ROW_INDEX is not None
        and self.DEFAULT_KEY_BYTES_INDICES is not None
    ):
        return self.DEFAULT_ROW_INDEX, self.DEFAULT_KEY_BYTES_INDICES
    return await original_get_indices(self, home_page_response, session, headers)

tx_mod.ClientTransaction.get_indices = patched_get_indices
# ── End Patch ─────────────────────────────────────────────────

load_dotenv()

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
MIN_FAVORITES = 20
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
        "likes":     source.favorite_count,
        "retweets":  source.retweet_count,
        "replies":   source.reply_count,
        "views":     source.view_count,
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