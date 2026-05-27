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
import sys
from datetime import datetime

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

# ─────────────────────────────────────────────────────────────
# SCRAPER
# ─────────────────────────────────────────────────────────────

from scraper import main as run_scraper

# ─────────────────────────────────────────────────────────────
# GEMINI RANKING
# ─────────────────────────────────────────────────────────────

class RankedTweet(BaseModel):
    id: str = Field(description="The ID of the tweet")
    summary: str = Field(description="A clean, professional summary of the development/news in English")

class RankingResponse(BaseModel):
    top_tweets: list[RankedTweet] = Field(description="List of top 5 tweets ranked by importance/impact of the AI development")

def rank_with_gemini(tweets: list[dict]) -> list[tuple[dict, str]]:
    """Rank tweets using Gemini AI and return a list of tuples containing (tweet_dict, summary)."""
    if not tweets:
        return []

    # Initialize Gemini client
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[!] GEMINI_API_KEY is not set in .env")
        return [(t, t["text"]) for t in tweets[:TOP_N]]

    client = genai.Client(api_key=api_key)

    # Format the tweets for the model prompt
    tweets_text = ""
    for t in tweets:
        tweets_text += f"- ID: {t['id']} | Author: @{t['author']} | Likes: {t['likes']} | RTs: {t['retweets']}\nText: {t['text']}\n\n"

    prompt = f"""
You are an expert AI researcher and tech journalist.
Below is a list of tweets collected from X/Twitter about recent AI developments and news.
Analyze them and select the top 5 most important and impactful AI news developments.
Avoid duplicate developments (e.g. multiple tweets talking about the same release).
For each selected tweet, write a clean, professional, and engaging summary (1-2 sentences) of the announcement/development in English.

Tweets list:
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
            summary = rt.get("summary", "")
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
            return [(t, t["text"]) for t in tweets[:TOP_N]]
            
        return ranked_list[:TOP_N]

    except Exception as e:
        print(f"[!] Error ranking with Gemini: {e}")
        return [(t, t["text"]) for t in tweets[:TOP_N]]

# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def telegram_post(text: str, chat_id: str) -> bool:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id":                  chat_id,
        "text":                     text,
        "parse_mode":               "HTML",
        "disable_web_page_preview": False,
    }
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
    body    = html.escape(summary)
    return f"<b>{rank}.</b>  @{author}\n{body}\n\n{url}"


def format_header() -> str:
    today = datetime.now().strftime("%d %b %Y")
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
    tweets = await run_scraper()
    if not tweets:
        print("  [!] No tweets returned. Exiting.")
        return
    print(f"  {len(tweets)} tweets collected.")

    # 2. Rank with Gemini
    print("\n[2/3] Ranking and summarizing with Gemini...")
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
        ok = telegram_post(format_message(rank, tweet, summary), chat_id)
        print(f"  [{'ok' if ok else '!!'}] {rank}. @{tweet['author']}")

    print("\n  Done.")


if __name__ == "__main__":
    asyncio.run(main())
