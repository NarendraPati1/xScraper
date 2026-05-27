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
import re
import sys
from datetime import datetime

from dotenv import load_dotenv

# ── Monkey-patch: fix 'ClientTransaction has no attribute key' ──
# Twitter periodically changes ondemand.s.js structure, breaking twikit's
# regex parser. This patch keeps things working until twikit releases a fix.
try:
    import twikit.x_client_transaction.transaction as _tx_mod
    _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
        r',(\d+):function\(\w+,\w+\)\{return "\w+"\}'
    )
except Exception:
    pass  # If patch fails, proceed normally and hope for the best

# Broader twikit compatibility patch for newer X responsive-web chunks.
try:
    import twikit.x_client_transaction.transaction as _tx_mod

    _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
        r"""['|\"]{1}ondemand\.s['|\"]{1}:\s*['|\"]{1}([\w]*)['|\"]{1}""",
        flags=(re.VERBOSE | re.MULTILINE),
    )

    async def _patched_get_indices(self, home_page_response, session, headers):
        response = self.validate_response(home_page_response) or self.home_page_response
        response_text = str(response)
        ondemand_urls = []

        ondemand_urls.extend(
            re.findall(
                r"https://abs\.twimg\.com/responsive-web/client-web/ondemand\.s\.[^\"']+?\.js",
                response_text,
            )
        )

        direct_hashes = re.findall(r"ondemand\.s\.([a-zA-Z0-9_-]+?)a?\.js", response_text)
        ondemand_urls.extend(
            f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{chunk_hash}a.js"
            for chunk_hash in direct_hashes
        )

        legacy_match = _tx_mod.ON_DEMAND_FILE_REGEX.search(response_text)
        if legacy_match:
            ondemand_urls.append(
                f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{legacy_match.group(1)}a.js"
            )

        chunk_id_matches = re.findall(r"(\d+):[\"']ondemand\.s[\"']", response_text)
        for chunk_id in chunk_id_matches:
            hash_match = re.search(rf"\b{chunk_id}:[\"']([a-zA-Z0-9_-]+)[\"']", response_text)
            if hash_match:
                ondemand_urls.append(
                    f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{hash_match.group(1)}a.js"
                )

        seen = set()
        for ondemand_url in ondemand_urls:
            if ondemand_url in seen:
                continue
            seen.add(ondemand_url)
            ondemand_response = await session.request(method="GET", url=ondemand_url, headers=headers)
            key_byte_indices = [
                int(match.group(1))
                for match in re.finditer(r"\(\w{1,3}\[(\d{1,2})\],\s*16\)", ondemand_response.text)
            ]
            if key_byte_indices:
                return key_byte_indices[0], key_byte_indices[1:]

        raise Exception("Couldn't get KEY_BYTE indices")

    _original_init = _tx_mod.ClientTransaction.init

    async def _patched_init(self, session, headers):
        try:
            await _original_init(self, session, headers)
        except Exception:
            self.home_page_response = None
            self.key = None
            self.key_bytes = None
            self.animation_key = None
            raise

    _tx_mod.ClientTransaction.get_indices = _patched_get_indices
    _tx_mod.ClientTransaction.init = _patched_init
except Exception:
    pass  # If patch fails, proceed normally and let twikit report the error.

from twikit import Client

# X sometimes omits optional user fields from GraphQL responses. Twikit 2.3.3
# treats some of them as required, which can drop otherwise valid tweets.
try:
    import twikit.user as _user_mod

    def _patched_user_init(self, client, data):
        self._client = client
        legacy = data.get("legacy") or {}
        entities = legacy.get("entities") or {}
        description_entities = entities.get("description") or {}
        url_entities = entities.get("url") or {}

        self.id = data.get("rest_id") or data.get("id") or legacy.get("id_str", "")
        self.created_at = legacy.get("created_at", "")
        self.name = legacy.get("name", "")
        self.screen_name = legacy.get("screen_name", "")
        self.profile_image_url = legacy.get("profile_image_url_https", "")
        self.profile_banner_url = legacy.get("profile_banner_url")
        self.url = legacy.get("url")
        self.location = legacy.get("location", "")
        self.description = legacy.get("description", "")
        self.description_urls = description_entities.get("urls", [])
        self.urls = url_entities.get("urls", [])
        self.pinned_tweet_ids = legacy.get("pinned_tweet_ids_str", [])
        self.is_blue_verified = data.get("is_blue_verified", False)
        self.verified = legacy.get("verified", False)
        self.possibly_sensitive = legacy.get("possibly_sensitive", False)
        self.can_dm = legacy.get("can_dm", False)
        self.can_media_tag = legacy.get("can_media_tag", False)
        self.want_retweets = legacy.get("want_retweets", False)
        self.default_profile = legacy.get("default_profile", False)
        self.default_profile_image = legacy.get("default_profile_image", False)
        self.has_custom_timelines = legacy.get("has_custom_timelines", False)
        self.followers_count = legacy.get("followers_count", 0)
        self.fast_followers_count = legacy.get("fast_followers_count", 0)
        self.normal_followers_count = legacy.get("normal_followers_count", 0)
        self.following_count = legacy.get("friends_count", 0)
        self.favourites_count = legacy.get("favourites_count", 0)
        self.listed_count = legacy.get("listed_count", 0)
        self.media_count = legacy.get("media_count", 0)
        self.statuses_count = legacy.get("statuses_count", 0)
        self.is_translator = legacy.get("is_translator", False)
        self.translator_type = legacy.get("translator_type", "")
        self.profile_interstitial_type = legacy.get("profile_interstitial_type", "")
        self.withheld_in_countries = legacy.get("withheld_in_countries", [])
        self.protected = legacy.get("protected", False)

    _user_mod.User.__init__ = _patched_user_init
except Exception:
    pass

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
# MAIN SCRAPER
# ─────────────────────────────────────────────

async def main():
    client = Client("en-US")

    # ── Login or reuse saved cookies ──────────
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
