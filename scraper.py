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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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

from twikit import Client, Unauthorized, Forbidden, NotFound

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
    "DrJimFan",           # Jim Fan (NVIDIA AI)
    "AndrewYNg",          # Andrew Ng
    "demishassabis",      # Demis Hassabis (DeepMind)
    "AlphaSignalAI",      # AlphaSignal (AI news)
    "rowancheung",        # Rowan Cheung (AI news editor)
    "AravSrinivas",       # Aravind Srinivas (Perplexity CEO)
    "gdb",                # Greg Brockman (OpenAI Co-founder)
    "ilyasut",            # Ilya Sutskever (SSI Co-founder)
]

# Search queries for latest AI news
SEARCH_QUERIES = [
    '"AI model release" OR "LLM release" OR "new AI model" lang:en min_faves:50 -filter:replies',
    '"frontier model" OR "open weights" OR "model benchmark" lang:en min_faves:30 -filter:replies',
    '"AI paper" OR "research paper" OR arxiv lang:en min_faves:20 -filter:replies',
    'GPT OR Claude OR Gemini OR Llama OR Mistral release lang:en min_faves:30 -filter:replies',
    'agentic OR "AI agent" OR "autonomous agent" release lang:en min_faves:30 -filter:replies',
    '"text-to-video" OR "generative video" OR "video model" OR "text-to-image" lang:en min_faves:40 -filter:replies',
    '"multimodal model" OR "vision language model" OR "VLM" lang:en min_faves:30 -filter:replies',
]

# Minimum engagement to filter noise
MIN_FAVORITES = 20
MAX_TWEETS_PER_QUERY = 10   # twikit max is 20 per call
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "3"))
SAVE_TWEETS_JSON = os.getenv("SAVE_TWEETS_JSON", "0").lower() in {"1", "true", "yes"}

AI_KEYWORDS = {
    "ai", "artificial intelligence", "llm", "gpt", "chatgpt", "openai",
    "claude", "anthropic", "gemini", "deepmind", "llama", "mistral",
    "hugging face", "huggingface", "model", "benchmark", "eval",
    "inference", "agent", "agents", "codex", "transformer", "token",
    "reasoning", "multimodal", "weights", "dataset", "arxiv", "paper",
    "research", "fine-tune", "finetune", "safety", "alignment",
}

NOISE_KEYWORDS = {
    "epstein", "trump", "congressman", "mosque", "shooting", "election",
    "senate", "war", "gaza", "ukraine",
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def format_tweet(tweet) -> dict:
    """Extract the fields we care about from a Tweet object."""
    # Unwrap retweets so we get the original content
    source = tweet.retweeted_tweet if tweet.retweeted_tweet else tweet
    
    text = source.text or ""
    preview_url = f"https://twitter.com/{source.user.screen_name}/status/{source.id}"
    
    # Retrieve quote tweet if available
    is_quote = getattr(source, "is_quote_status", False) or getattr(source, "is_quote", False)
    quote_obj = getattr(source, "quote", None)
    if is_quote and quote_obj:
        quoted_text = quote_obj.text or ""
        quoted_author = ""
        if getattr(quote_obj, "user", None):
            quoted_author = getattr(quote_obj.user, "screen_name", "") or getattr(quote_obj.user, "username", "")
        if quoted_text:
            text += f"\n\n[Quoted from @{quoted_author}]: {quoted_text}"
            if quoted_author and quote_obj.id:
                preview_url = f"https://twitter.com/{quoted_author}/status/{quote_obj.id}"

    return {
        "id":          source.id,
        "author":      source.user.screen_name,
        "text":        text,
        "likes":       source.favorite_count,
        "retweets":    source.retweet_count,
        "created":     source.created_at,
        "url":         f"https://twitter.com/{source.user.screen_name}/status/{source.id}",
        "preview_url": preview_url,
    }


def print_tweet(t: dict, index: int):
    print(f"\n{'─'*60}")
    print(f"[{index}] @{t['author']}  ·  {t['created']}")
    print(f"    {t['text'][:200]}{'...' if len(t['text']) > 200 else ''}")
    print(f"    ❤ {t['likes']}  🔁 {t['retweets']}  💬 {t['replies']}  👁 {t['views']}")
    print(f"    {t['url']}")


def created_datetime(tweet: dict) -> datetime | None:
    try:
        return parsedate_to_datetime(tweet["created"])
    except Exception:
        return None


def is_recent(tweet: dict, days: int = RECENT_DAYS) -> bool:
    created = created_datetime(tweet)
    if created is None:
        return True
    return (datetime.now(timezone.utc) - created).days <= days


def is_english(tweet: dict) -> bool:
    """Return True if the tweet text is predominantly Latin/ASCII (i.e. English)."""
    text = tweet.get("text", "")
    if not text:
        return True
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return (ascii_chars / len(text)) >= 0.80


def ai_relevance_score(tweet: dict) -> int:
    text = f"{tweet['author']} {tweet['text']}".lower()
    if any(keyword in text for keyword in NOISE_KEYWORDS):
        return 0
    return sum(1 for keyword in AI_KEYWORDS if keyword in text)


def should_keep_tweet(tweet: dict) -> bool:
    return is_recent(tweet) and ai_relevance_score(tweet) > 0 and is_english(tweet)


def print_tweet_summary(tweet: dict, index: int) -> None:
    print(f"\n[{index}] @{tweet['author']}  -  {tweet['created']}")
    print(f"    {tweet['text'][:200]}{'...' if len(tweet['text']) > 200 else ''}")
    print(f"    {tweet['url']}")


async def scrape_custom_query(query: str, count: int = 15, verbose: bool = True) -> list[dict]:
    """Scrape tweets for a custom search query from X. No filtering applied — returns raw X results."""
    client = Client("en-US")
    if os.path.exists(COOKIES_FILE):
        if verbose:
            print(f"[+] Loading saved cookies from {COOKIES_FILE}")
        client.load_cookies(COOKIES_FILE)
    else:
        raise RuntimeError(
            "No cookies.json found. Cannot login from a server IP (Cloudflare blocks it). "
            "Run scraper.py locally to generate cookies.json, then scp it to the server."
        )

    if verbose:
        print(f"[+] Searching X for custom query: '{query}'")

    try:
        results = await client.search_tweet(query, "Latest", count=count)
    except Exception as e:
        if verbose:
            print(f"[!] Custom search failed: {e}")
        raise e

    tweets_list = []
    seen_ids = set()
    for tweet in results:
        t = format_tweet(tweet)
        tid = str(t["id"])
        if tid not in seen_ids:
            seen_ids.add(tid)
            tweets_list.append(t)

    if verbose:
        print(f"[+] Found {len(tweets_list)} tweets for query '{query}'.")

    return tweets_list


# ─────────────────────────────────────────────
# MAIN SCRAPER
# ─────────────────────────────────────────────

async def main(verbose: bool = True, save_json: bool | None = None):
    client = Client("en-US")
    save_json = SAVE_TWEETS_JSON if save_json is None else save_json

    # ── Load saved cookies (no validation ping — it triggers Cloudflare blocks) ──
    # Login from datacenter/server IPs is always blocked by Cloudflare.
    # Cookies must be generated locally and copied to the server manually.
    if os.path.exists(COOKIES_FILE):
        if verbose:
            print(f"[+] Loading saved cookies from {COOKIES_FILE}")
        client.load_cookies(COOKIES_FILE)
        # No validation ping — just trust the cookies and let real requests fail
        # naturally if they're expired. Individual failures are handled per-query below.
        if verbose:
            print("[+] Cookies loaded. Proceeding with scrape.")
    else:
        # No cookies on server = cannot login (Cloudflare blocks datacenter IPs).
        # Generate cookies locally: run `python scraper.py` on your local machine,
        # then copy cookies.json to the server.
        raise RuntimeError(
            "No cookies.json found. Cannot login from a server IP (Cloudflare blocks it). "
            "Run scraper.py locally to generate cookies.json, then scp it to the server."
        )

    all_tweets = {}   # keyed by tweet ID to auto-deduplicate

    # ── 1. Search queries ──────────────────────
    if verbose:
        print("\n[+] Running search queries...")
    for query in SEARCH_QUERIES:
        if verbose:
            print(f"    Searching: {query}")
        try:
            results = await client.search_tweet(query, "Latest", count=MAX_TWEETS_PER_QUERY)
            for tweet in results:
                t = format_tweet(tweet)
                if t["likes"] >= MIN_FAVORITES and should_keep_tweet(t) and t["id"] not in all_tweets:
                    all_tweets[t["id"]] = t
        except (Unauthorized, Forbidden, NotFound) as e:
            print(f"    [!] Query failed (auth/not-found): {e} — skipping query, cookies kept.")
        except Exception as e:
            print(f"    [!] Query failed: {e}")
        await asyncio.sleep(2)   # be polite between requests

    # ── 2. Account timelines ──────────────────
    if verbose:
        print("\n[+] Fetching account timelines...")
    for username in AI_ACCOUNTS:
        if verbose:
            print(f"    @{username}")
        try:
            user = await client.get_user_by_screen_name(username)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=5)
            for tweet in tweets:
                t = format_tweet(tweet)
                if should_keep_tweet(t) and t["id"] not in all_tweets:
                    all_tweets[t["id"]] = t
        except (Unauthorized, Forbidden, NotFound) as e:
            print(f"    [!] Failed for @{username} (auth/not-found): {e} — skipping, cookies kept.")
        except Exception as e:
            print(f"    [!] Failed for @{username}: {e}")
        await asyncio.sleep(1.5)

    # ── Results ───────────────────────────────
    tweets_list = sorted(
        all_tweets.values(),
        key=lambda x: (
            ai_relevance_score(x),
            created_datetime(x) or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Found {len(tweets_list)} unique AI tweets")
        print(f"{'='*60}")

        for i, t in enumerate(tweets_list[:20], 1):
            print_tweet_summary(t, i)

    # ── Save to JSON ──────────────────────────
    if save_json:
        output_file = f"ai_tweets_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(tweets_list, f, indent=2, ensure_ascii=False)
        if verbose:
            print(f"\n[+] Full results saved to {output_file}")

    return tweets_list


if __name__ == "__main__":
    asyncio.run(main())
