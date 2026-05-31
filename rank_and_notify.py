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
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(IST)

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

load_dotenv()
sys.stdout.reconfigure(encoding="utf-8")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
TOP_N = 5
MAX_GEMINI_CANDIDATES = int(os.getenv("MAX_GEMINI_CANDIDATES", "60"))

PREFERENCES_FILE = Path("user_preferences.json")

# ─────────────────────────────────────────────────────────────
# USER PREFERENCES
# ─────────────────────────────────────────────────────────────

def load_preferences(chat_id: str | None = None) -> dict:
    """Load user preferences from disk."""
    suffix = f"_{chat_id}" if chat_id else ""
    file_path = Path(f"user_preferences{suffix}.json")
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("liked_topics", [])
                data.setdefault("liked_tweets", [])
                data.setdefault("disliked_topics", [])
                data.setdefault("disliked_tweets", [])
                data.setdefault("shown_tweets", [])
                return data
        except Exception:
            pass
    return {"liked_topics": [], "liked_tweets": [], "disliked_topics": [], "disliked_tweets": [], "shown_tweets": []}


def save_preferences(prefs: dict, chat_id: str | None = None) -> None:
    """Save user preferences to disk."""
    suffix = f"_{chat_id}" if chat_id else ""
    file_path = Path(f"user_preferences{suffix}.json")
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Could not save preferences: {e}")


def add_shown_tweets(tweet_ids: list[str], chat_id: str | None = None) -> None:
    """Record shown tweet IDs to prevent showing them again soon (rolling window of 40)."""
    prefs = load_preferences(chat_id)
    prefs.setdefault("shown_tweets", [])

    existing = prefs["shown_tweets"]
    for tid in tweet_ids:
        tid_str = str(tid)
        if tid_str in existing:
            existing.remove(tid_str)
        existing.append(tid_str)

    if len(existing) > 40:
        existing = existing[-40:]
    prefs["shown_tweets"] = existing

    save_preferences(prefs, chat_id)


def add_liked_tweet(tweet: dict, topic: str, chat_id: str | None = None) -> None:
    """Add a liked tweet and its extracted topic to preferences (rolling list of last 10 likes)."""
    prefs = load_preferences(chat_id)

    # Remove any existing tweet with this ID so we can place it at the end (newest)
    prefs["liked_tweets"] = [t for t in prefs["liked_tweets"] if str(t.get("id")) != str(tweet.get("id"))]

    # Append new like
    prefs["liked_tweets"].append({
        "id": str(tweet.get("id")),
        "author": tweet.get("author", ""),
        "text_snippet": (tweet.get("text", ""))[:200],
        "topic": topic,
        "liked_at": datetime.now().isoformat(),
    })

    # Keep only the rolling window of the last 10 liked tweets (newest are at the end)
    if len(prefs["liked_tweets"]) > 10:
        prefs["liked_tweets"] = prefs["liked_tweets"][-10:]

    # Rebuild liked_topics ordered by recency (newest first)
    seen_topics = set()
    unique_topics = []
    # Loop backwards through liked tweets (most recent first)
    for t in reversed(prefs["liked_tweets"]):
        t_name = t.get("topic", "")
        if t_name and t_name.lower() not in seen_topics:
            seen_topics.add(t_name.lower())
            unique_topics.append(t_name)

    prefs["liked_topics"] = unique_topics

    save_preferences(prefs, chat_id)


def remove_liked_topic(topic: str, chat_id: str | None = None) -> bool:
    """Remove a specific topic from the user's tracked list. Returns True if found and removed."""
    prefs = load_preferences(chat_id)
    topic_lower = topic.strip().lower()

    # Remove from liked_tweets whose topic matches
    original_len = len(prefs["liked_tweets"])
    prefs["liked_tweets"] = [
        t for t in prefs["liked_tweets"]
        if t.get("topic", "").lower() != topic_lower
    ]

    # Rebuild liked_topics
    seen_topics: set = set()
    unique_topics = []
    for t in reversed(prefs["liked_tweets"]):
        t_name = t.get("topic", "")
        if t_name and t_name.lower() not in seen_topics:
            seen_topics.add(t_name.lower())
            unique_topics.append(t_name)
    prefs["liked_topics"] = unique_topics

    save_preferences(prefs, chat_id)
    return len(prefs["liked_tweets"]) < original_len


def add_disliked_tweet(tweet: dict, topic: str, chat_id: str | None = None) -> None:
    """Add a disliked tweet and its topic to preferences (rolling list of last 20 dislikes)."""
    prefs = load_preferences(chat_id)
    prefs.setdefault("disliked_tweets", [])

    # Remove any existing tweet with this ID
    prefs["disliked_tweets"] = [t for t in prefs["disliked_tweets"] if str(t.get("id")) != str(tweet.get("id"))]

    # Append new dislike
    prefs["disliked_tweets"].append({
        "id": str(tweet.get("id")),
        "author": tweet.get("author", ""),
        "text_snippet": (tweet.get("text", ""))[:200],
        "topic": topic,
        "disliked_at": datetime.now().isoformat(),
    })

    if len(prefs["disliked_tweets"]) > 20:
        prefs["disliked_tweets"] = prefs["disliked_tweets"][-20:]

    # Also add the topic to disliked_topics
    disliked_topics = prefs.get("disliked_topics", [])
    if topic.lower() not in [d.lower() for d in disliked_topics]:
        disliked_topics.append(topic)
    prefs["disliked_topics"] = disliked_topics[-20:]

    save_preferences(prefs, chat_id)


async def curate_feed_locally(
    global_ranked_results: list[tuple[dict, str]],
    chat_id: str,
    top_n: int = 5,
) -> tuple[list[tuple[dict, str]], list[tuple[dict, str]]]:
    """Curate the globally pre-ranked pool for a specific user.

    If the user has liked topics, makes a single Gemini call to semantically
    match the 50 pre-ranked tweets against their interests.

    If the user has no preferences, returns the global top N as-is.

    Disliked topics are always suppressed before Gemini sees the candidates.

    Returns:
        (top_n results, remaining more_pool — both as list of (tweet, summary))
    """
    prefs = load_preferences(chat_id)
    liked_tweets_meta = prefs.get("liked_tweets", [])   # ordered oldest→newest
    liked_topics      = prefs.get("liked_topics", [])   # newest first
    disliked_topics   = [d.lower() for d in prefs.get("disliked_topics", [])]
    shown_tweets      = {str(tid) for tid in prefs.get("shown_tweets", [])}

    # ── Step 1: Filter out disliked topics from the pool ──────
    def _is_disliked(tweet: dict, summary: str) -> bool:
        content = (tweet.get("text", "") + " " + summary).lower()
        for dtopic in disliked_topics:
            dwords = [w for w in dtopic.split() if len(w) > 2]
            if dwords and sum(1 for w in dwords if w in content) / len(dwords) >= 0.6:
                return True
        return False

    filtered_pool = [(t, s) for t, s in global_ranked_results if not _is_disliked(t, s)]

    # Further filter out recently shown tweets
    unseen_pool = [(t, s) for t, s in filtered_pool if str(t["id"]) not in shown_tweets]

    # Fallback: if we don't have enough unseen tweets, supplement with the oldest shown ones
    if len(unseen_pool) >= top_n:
        candidates = unseen_pool
    else:
        shown_order = {str(tid): idx for idx, tid in enumerate(prefs.get("shown_tweets", []))}
        shown_candidates = [(t, s) for t, s in filtered_pool if str(t["id"]) in shown_tweets]
        shown_candidates.sort(key=lambda x: shown_order.get(str(x[0]["id"]), -1))
        candidates = unseen_pool + shown_candidates

    # ── Step 2: No preferences → randomised slice from top pool ─
    if not liked_topics:
        # Pick from the top-20 with a random shuffle so each digest feels fresh
        pool_size = min(20, len(candidates))
        pool = candidates[:pool_size]
        random.shuffle(pool)
        top  = pool[:top_n]
        # Rest = shuffled remainder of pool + everything beyond pool_size
        rest = pool[top_n:] + candidates[pool_size:]
        add_shown_tweets([str(t["id"]) for t, _ in top], chat_id)
        return top, rest

    # ── Step 3: Semantic re-ranking via Gemini ─────────────────
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Fallback: return global order
        top = candidates[:top_n]
        rest = candidates[top_n:]
        add_shown_tweets([str(t["id"]) for t, _ in top], chat_id)
        return top, rest

    # Build age-aware topic string (most recent topics listed first with weight hint)
    now = _now_ist()
    topic_lines = []
    for lt in reversed(liked_tweets_meta):  # newest first
        topic = lt.get("topic", "").strip()
        if not topic:
            continue
        try:
            age_days = (now - datetime.fromisoformat(lt.get("liked_at", ""))).days
        except Exception:
            age_days = 0
        recency = "very recently" if age_days < 2 else f"{age_days} days ago"
        topic_lines.append(f"  - \"{topic}\" (liked {recency})")

    topics_block = "\n".join(topic_lines)

    # Build concise candidate list: just ID + one-line summary
    candidate_lines = []
    tweets_map: dict[str, tuple[dict, str]] = {}
    for t, s in candidates:
        tid = str(t["id"])
        tweets_map[tid] = (t, s)
        short = (" ".join(s.split()))[:200]
        candidate_lines.append(f"ID:{tid} | {short}")

    candidates_block = "\n".join(candidate_lines)

    prompt = f"""You are personalizing an AI news feed.

The user has liked these topics (ordered from most to least recent — weight recent ones higher):
{topics_block}

Below are {len(candidates)} pre-curated AI news tweets (ID | summary).
Pick the {top_n} tweets that are MOST semantically relevant to the user's interests.
Semantic relevance means conceptual similarity, not just exact keyword matches.

For each selected tweet, reuse its existing summary but prefix it with "🔄 Update on <matched topic>: " if it is a follow-up or update on one of the user's tracked topics. Otherwise keep the summary as-is.

Return ONLY a JSON array of {top_n} objects in order of relevance:
[{{"id": "<tweet_id>", "summary": "<summary>"}}]

Candidates:
{candidates_block}
"""

    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        import re as _re
        raw = (response.text or "").strip()
        # Strip markdown code fences if present
        raw = _re.sub(r"^```[a-z]*\n?", "", raw).rstrip("` \n")
        ranked_ids: list[dict] = json.loads(raw)

        top: list[tuple[dict, str]] = []
        seen: set = set()
        for item in ranked_ids:
            tid = str(item.get("id", "")).strip()
            custom_summary = item.get("summary", "")
            if tid in tweets_map and tid not in seen:
                orig_tweet, orig_summary = tweets_map[tid]
                final_summary = custom_summary if custom_summary else orig_summary
                top.append((orig_tweet, final_summary))
                seen.add(tid)
            if len(top) >= top_n:
                break

        # Fill up to top_n from global order if Gemini returned fewer
        if len(top) < top_n:
            for t, s in candidates:
                if str(t["id"]) not in seen:
                    top.append((t, s))
                if len(top) >= top_n:
                    break

        rest = [(t, s) for t, s in candidates if str(t["id"]) not in {str(x["id"]) for x, _ in top}]
        add_shown_tweets([str(x["id"]) for x, _ in top], chat_id)
        return top, rest

    except Exception as e:
        print(f"[!] Semantic curation failed: {e}. Falling back to global order.")
        top = candidates[:top_n]
        rest = candidates[top_n:]
        add_shown_tweets([str(t["id"]) for t, _ in top], chat_id)
        return top, rest


# ─────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────

from scraper import main as run_scraper

# ─────────────────────────────────────────────────────────────
# GEMINI RANKING
# ─────────────────────────────────────────────────────────────

class RankedTweet(BaseModel):
    id: str = Field(description="The ID of the tweet")
    summary: str = Field(description="A short factual summary of the AI development")

class RankingResponse(BaseModel):
    top_tweets: list[RankedTweet] = Field(description="List of top 5 tweets ranked by importance/impact of the AI development")


def _candidate_payload(tweet: dict) -> str:
    text = " ".join(tweet["text"].split())
    if len(text) > 500:
        text = text[:497].rstrip() + "..."
    return (
        f"ID: {tweet['id']}\n"
        f"Author: @{tweet['author']}\n"
        f"Date: {tweet['created']}\n"
        f"Text: {text}\n"
    )


def _preferences_context(prefs: dict) -> str:
    """Build a preferences context block to inject into Gemini prompts.
    Prioritizes newer topics, as user interests shift over time.
    """
    topics = prefs.get("liked_topics", [])
    if not topics:
        return ""
    # topics is ordered newest first
    topics_str = ", ".join(f'"{t}"' for t in topics)
    return (
        f"\nUser Preferences (topics user has liked, ordered from newest/most-relevant to oldest):\n"
        f"  Tracked topics: {topics_str}\n"
        f"Please prioritize candidate tweets that are updates, progress reports, or directly related to these topics, "
        f"with a stronger emphasis on the newer/first-listed topics.\n"
        f"When curating/summarizing, if a tweet is an update or follow-up on any of these tracked topics, "
        f'prefix its summary with "🔄 Update: " so the user knows it is a follow-up to something they care about.\n'
    )


async def extract_topic_with_gemini(tweet: dict) -> str:
    """Use Gemini to extract a short 2-4 word topic label from a liked tweet."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Fallback: use first few words of the tweet as the topic
        words = tweet.get("text", "Liked tweet").split()
        return " ".join(words[:4])

    client = genai.Client(api_key=api_key)
    text = " ".join(tweet.get("text", "").split())[:500]
    prompt = (
        f"Extract a short, descriptive topic label (2-4 words) for this AI news tweet. "
        f"Return ONLY the topic label, no explanation or punctuation.\n\n"
        f"Tweet: {text}"
    )

    try:
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        topic = (response.text or "").strip().strip('"').strip("'")
        # Cap at reasonable length
        if len(topic) > 60:
            topic = topic[:60]
        return topic or "AI Development"
    except Exception as e:
        print(f"[!] Error extracting topic: {e}")
        words = tweet.get("text", "AI Development").split()
        return " ".join(words[:4])


def rank_with_gemini(tweets: list[dict], chat_id: str | None = None, count: int = TOP_N) -> list[tuple[dict, str]]:
    """Rank tweets using Gemini AI and return a list of tuples containing (tweet_dict, summary).
    
    Injects user preferences if chat_id is provided, and curates up to `count` tweets.
    """
    if not tweets:
        return []

    # Initialize Gemini client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[!] GEMINI_API_KEY is not set in .env")
        return [(t, t["text"]) for t in tweets[:count]]

    client = genai.Client(api_key=api_key)

    # Shuffle candidates so Gemini sees a different ordering each run → more variety
    pool = tweets[:max(MAX_GEMINI_CANDIDATES, count + 20)]
    random.shuffle(pool)
    candidates = pool
    tweets_text = "\n".join(_candidate_payload(t) for t in candidates)

    # Load preferences for personalization
    prefs = load_preferences(chat_id)
    prefs_context = _preferences_context(prefs)

    prompt = f"""
You are curating a concise AI news digest for builders and researchers.

Select exactly {count} tweets that are the most useful AI developments from the candidate list.

Selection rules:
- Prefer concrete AI releases, model updates, benchmarks, research results, product launches, safety/security findings, and developer tooling.
- Reject off-topic politics, tragedy, generic opinions, memes, engagement bait, personal updates without technical/news value, and duplicates.
- Prefer recent, specific updates over older viral tweets.
- Do not choose more than one tweet about the same underlying development.
{prefs_context}
For each selected tweet, write a short, Telegram-friendly summary. Write all summaries in strict English, regardless of the language of the source tweet. Keep it concise, factual, and easy to scan. No hype.

Candidate tweets:
{tweets_text}
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RankingResponse,
            ),
        )
        
        data = json.loads(response.text)
        ranked_list = []
        tweets_map = {str(t["id"]): t for t in tweets}
        
        for rt in data.get("top_tweets", []):
            tweet_id_str = str(rt.get("id")).strip()
            summary = " ".join(rt.get("summary", "").split())
            if tweet_id_str in tweets_map:
                ranked_list.append((tweets_map[tweet_id_str], summary))
            else:
                # Fallback matching logic
                matched = False
                for tid, t in tweets_map.items():
                    if tweet_id_str in tid or tid in tweet_id_str:
                        ranked_list.append((t, summary))
                        matched = True
                        break
                if not matched:
                    # Skip if no ID matches
                    pass
        
        if not ranked_list:
            print("[!] Gemini returned no valid ranked tweets. Falling back to default top list.")
            return [(t, t["text"]) for t in tweets[:count]]
            
        return ranked_list[:count]

    except Exception as e:
        print(f"[!] Error ranking with Gemini: {e}")
        return [(t, t["text"]) for t in tweets[:count]]

def summarize_tweets(tweets: list[dict], chat_id: str | None = None) -> dict[str, str]:
    """Generate concise, scanable, factual English summaries for each input tweet using Gemini.
    
    Injects user preferences so follow-ups on liked topics are flagged with 🔄.
    """
    if not tweets:
        return {}

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {str(t["id"]): t["text"] for t in tweets}

    client = genai.Client(api_key=api_key)
    tweets_text = "\n".join(_candidate_payload(t) for t in tweets)

    # Load preferences for personalization
    prefs = load_preferences(chat_id)
    prefs_context = _preferences_context(prefs)

    prompt = f"""
You are summarizing AI news updates for builders and researchers.

For each of the following tweets, write a short, Telegram-friendly summary. 
Write all summaries in strict English, regardless of the language of the source tweet. 
Keep it concise, factual, and easy to scan. No hype. Do not add intro/outro text.
{prefs_context}
Tweets to summarize:
{tweets_text}
"""

    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RankingResponse,
            ),
        )
        
        data = json.loads(response.text)
        summary_map = {}
        for rt in data.get("top_tweets", []):
            tweet_id_str = str(rt.get("id")).strip()
            summary = " ".join(rt.get("summary", "").split())
            summary_map[tweet_id_str] = summary

        # Ensure every tweet in the input has some entry (fallback to original text if missing)
        result = {}
        for t in tweets:
            tid = str(t["id"])
            if tid in summary_map:
                result[tid] = summary_map[tid]
            else:
                # Fallback to fuzzy search or original text
                matched = False
                for k, v in summary_map.items():
                    if k in tid or tid in k:
                        result[tid] = v
                        matched = True
                        break
                if not matched:
                    result[tid] = t["text"]
        return result

    except Exception as e:
        print(f"[!] Error summarizing with Gemini: {e}")
        return {str(t["id"]): t["text"] for t in tweets}


# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def telegram_post(text: str, chat_id: str, preview_url: str | None = None) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
    }
    if preview_url:
        data["link_preview_options"] = {"url": preview_url}
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

def format_message(rank: int, tweet: dict, summary: str) -> str:
    author  = tweet["author"]
    url     = tweet["url"]
    # Twitter API pre-encodes HTML entities (e.g. &gt; for >).
    # Unescape first so we don't double-encode them before re-escaping for Telegram.
    clean   = html.escape(html.unescape(" ".join((summary or "").split())))
    return f"<b>{rank}.</b>  @{author}\n{clean}\n\n{url}"


def format_header() -> str:
    today = _now_ist().strftime("%d %b %Y, %I:%M %p IST")
    return f"<b>AI News Digest</b>  —  {today}"

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main():
    print("─" * 60)
    print("  AI Tweet Digest  |  Scrape > Notify")
    print("─" * 60)

    # 1. Scrape
    print("\n[1/3] Scraping tweets...")
    tweets = await run_scraper(verbose=True)
    if not tweets:
        print("  [!] No tweets returned. Exiting.")
        return
    print(f"  {len(tweets)} tweets collected.")

    # 2. Rank with Gemini (preferences are injected inside)
    print("\n[2/3] Ranking and summarizing with Gemini...")
    prefs = load_preferences()
    topics = prefs.get("liked_topics", [])
    if topics:
        print(f"  [i] Personalizing with {len(topics)} tracked topic(s): {', '.join(topics)}")
    top = rank_with_gemini(tweets)
    print(f"  {len(top)} tweets selected and summarized.")

    # 3. Send
    print("\n[3/3] Sending to Telegram...")
    chat_id = TELEGRAM_CHAT_ID or detect_chat_id()
    if not chat_id:
        print("  [!] Could not detect chat ID. Send a message to your bot first.")
        return

    telegram_post(format_header(), chat_id)
    for rank, (tweet, summary) in enumerate(top, 1):
        ok = telegram_post(format_message(rank, tweet, summary), chat_id, preview_url=tweet["preview_url"])
        print(f"  [{'ok' if ok else '!!'}] {rank}. @{tweet['author']}")

    print("\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
