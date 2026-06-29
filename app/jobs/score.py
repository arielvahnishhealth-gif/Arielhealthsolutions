from app.db import session_scope
from app.models import Lead
from app.services.alerts import notify
from app.services.states import LICENSED_STATES, STATE_FULL_NAMES


WEIGHTS = {
    "private health insurance": 5.0,
    "private ppo": 5.0,
    "ppo plan": 5.0,
    "ppo health insurance": 5.0,
    "family ppo plan": 5.0,
    "nationwide ppo": 4.5,
    "executive health plan": 4.0,
    "hsa plan": 4.0,
    "hsa ppo": 4.0,
    "catastrophic coverage": 4.0,
    "short term medical": 3.5,
    "looking for health insurance": 3.0,
    "need health insurance": 3.0,
    "buy health insurance": 3.0,
    "aca health plan": 2.0,
    "obamacare": 2.0,
    "marketplace coverage": 2.0,
    "healthcare.gov help": 2.0,
    "silver plan": 1.5,
    "gold plan": 1.5,
    "lost coverage": 3.5,
    "turning 26": 3.5,
    "cobra is expensive": 3.0,
    "cobra too expensive": 3.0,
    "cobra alternative": 3.0,
    "laid off need health insurance": 3.5,
    "between jobs health insurance": 3.0,
    "health insurance broker": 2.5,
    "health insurance agent near me": 2.5,
    "self pay health insurance": 2.5,
}


def geo_boost(text: str) -> float:
    body = f" {str(text or '').lower()} "
    full_names = {
        STATE_FULL_NAMES[abbr].lower(): abbr
        for abbr in LICENSED_STATES
        if abbr in STATE_FULL_NAMES
    }
    for abbr in LICENSED_STATES:
        if f" {abbr.lower()} " in body:
            return 2.0
    for name in full_names:
        if name in body:
            return 2.0
    return 0.0


def intent_score(keywords: list[str], text: str) -> float:
    base = sum(WEIGHTS.get(keyword.lower(), 0.8) for keyword in set(keyword.lower() for keyword in keywords))
    length_bonus = min(len(text or "") / 200, 2.0)
    return base + geo_boost(text) + length_bonus


def infer_keywords(text: str, explicit: list[str] | None = None) -> list[str]:
    found = list(explicit or [])
    body = (text or "").lower()
    for keyword in WEIGHTS:
        if keyword in body and keyword not in found:
            found.append(keyword)
    return found


def upsert_lead(
    platform: str,
    url: str,
    message: str | None,
    handle: str | None,
    keywords: list[str],
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    location: str | None = None,
) -> dict[str, object]:
    with session_scope() as session:
        keywords = infer_keywords(message or "", keywords)
        score = intent_score(keywords, message or "")
        existing = session.query(Lead).filter_by(platform=platform, url=url).one_or_none()
        if existing:
            existing.score = max(existing.score, score)
            existing.keywords = sorted(set((existing.keywords or []) + keywords))
            existing.name = name or existing.name
            existing.handle = handle or existing.handle
            existing.message = message or existing.message
            existing.email = email or existing.email
            existing.phone = phone or existing.phone
            existing.location = location or existing.location
            lead = existing
        else:
            lead = Lead(
                name=name,
                platform=platform,
                url=url,
                message=message,
                handle=handle,
                email=email,
                phone=phone,
                location=location,
                keywords=sorted(set(keywords)),
                score=score,
            )
            session.add(lead)
        session.flush()
        if score >= 3.0:
            notify(lead)
        return {"id": lead.id, "platform": lead.platform, "url": lead.url, "score": lead.score}


def process_manual_lead(payload: dict):
    platform = str(payload.get("source") or "web").strip().lower().replace(" ", "_")
    return upsert_lead(
        platform or "web",
        payload["url"],
        payload.get("message"),
        payload.get("handle"),
        payload.get("keywords", []),
        payload.get("name"),
        payload.get("email"),
        payload.get("phone"),
        payload.get("location"),
    )
