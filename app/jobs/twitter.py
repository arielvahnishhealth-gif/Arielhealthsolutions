import os

import httpx

from app.jobs.score import upsert_lead
from app.services.keywords import GENERAL_INTENT, PRIVATE_INTENT


def run_once() -> None:
    token = os.getenv("TWITTER_BEARER_TOKEN")
    if not token:
        return
    seed = list(
        dict.fromkeys(
            PRIVATE_INTENT[:6]
            + GENERAL_INTENT[:4]
            + [
                "cobra too expensive",
                "cobra alternative",
                "self employed health insurance",
                "turning 26",
                "lost coverage",
            ]
        )
    )
    seed = seed[:10]
    query = "(" + " OR ".join([f'"{keyword}"' for keyword in seed]) + ") -is:retweet lang:en"
    url = "https://api.twitter.com/2/tweets/search/recent"
    params = {"query": query, "max_results": 30, "tweet.fields": "created_at,geo,author_id"}
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=20) as client:
        response = client.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            return
        for tweet in response.json().get("data", []):
            text = tweet.get("text", "")
            keywords = [keyword for keyword in seed if keyword.lower() in text.lower()]
            upsert_lead(
                "twitter",
                f"https://x.com/i/web/status/{tweet['id']}",
                text[:1000],
                tweet.get("author_id"),
                keywords,
            )
