import os

try:
    import praw
except ImportError:  # pragma: no cover - optional integration
    praw = None

from app.jobs.score import upsert_lead
from app.services.keywords import EXPANDED_KEYWORDS


def run_once() -> None:
    if not praw or not (os.getenv("REDDIT_CLIENT_ID") and os.getenv("REDDIT_CLIENT_SECRET")):
        return

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "health-leads-bot/1.0"),
    )
    subreddits = [
        "insurance",
        "personalfinance",
        "healthinsurance",
        "Medicare",
        "Entrepreneur",
        "healthcare",
        "smallbusiness",
        "selfemployed",
        "freelance",
        "RealEstate",
        "sales",
        "jobs",
        "povertyfinance",
    ]
    batch = 6
    keywords = list(EXPANDED_KEYWORDS)[:150]
    for i in range(0, len(keywords), batch):
        query = " OR ".join([f'"{keyword}"' for keyword in keywords[i : i + batch]])
        for subreddit in subreddits:
            for post in reddit.subreddit(subreddit).search(
                query=query, sort="new", time_filter="day", limit=25
            ):
                text = f"{post.title}\n{post.selftext or ''}"
                matched = [keyword for keyword in keywords if keyword.lower() in text.lower()]
                if matched:
                    author = post.author.name if post.author else "unknown"
                    upsert_lead(
                        "reddit",
                        f"https://reddit.com{post.permalink}",
                        text[:1000],
                        f"u/{author}",
                        matched[:20],
                    )
