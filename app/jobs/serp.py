import os

import httpx

from app.jobs.score import upsert_lead
from app.services.keywords import EXPANDED_KEYWORDS


def run_once() -> None:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return
    queries = [
        "site:reddit.com looking for health insurance",
        "site:reddit.com/r/healthinsurance need health insurance",
        "site:reddit.com/r/personalfinance cobra too expensive health insurance",
        "site:reddit.com/r/selfemployed health insurance",
        "site:quora.com private health insurance",
        "site:quora.com need health insurance self employed",
        "site:nextdoor.com health insurance agent",
        "looking for health insurance near me",
        "private PPO health insurance agent",
        "COBRA alternative health insurance",
        "self employed health insurance broker",
        "family PPO health insurance quote",
    ]
    with httpx.Client(timeout=20) as client:
        for query in queries:
            response = client.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": query, "api_key": key, "num": 10},
            )
            if response.status_code >= 400:
                continue
            for item in response.json().get("organic_results", []):
                snippet = item.get("snippet") or ""
                keywords = [
                    keyword for keyword in EXPANDED_KEYWORDS if keyword.lower() in snippet.lower()
                ]
                if item.get("link"):
                    upsert_lead("web", item["link"], snippet[:1000], None, keywords[:20])
