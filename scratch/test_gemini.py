import os
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

class RankedTweet(BaseModel):
    id: str = Field(description="The ID of the tweet")
    summary: str = Field(description="A clean, professional summary of the development/news in English")

class RankingResponse(BaseModel):
    top_tweets: list[RankedTweet] = Field(description="List of top 5 tweets ranked by importance/impact of the AI development")

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

try:
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents='Rank this list: 123, 456, 789. Choose 123 as the top one.',
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RankingResponse,
        ),
    )
    print("Response text:", response.text)
except Exception as e:
    print("Error:", e)
