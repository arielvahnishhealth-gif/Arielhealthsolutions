from app.services.states import LICENSED_STATES, STATE_FULL_NAMES


PRIVATE_INTENT = [
    "private health insurance",
    "private ppo",
    "ppo plan",
    "ppo health insurance",
    "family ppo plan",
    "nationwide ppo",
    "concierge health insurance",
    "executive health plan",
    "catastrophic coverage",
    "short term medical",
    "hsa plan",
    "hsa ppo",
    "family private plan",
    "private family coverage",
]

GENERAL_INTENT = [
    "looking for health insurance",
    "need health insurance",
    "buy health insurance",
    "healthcare plans",
    "medical coverage",
    "health insurance quotes",
    "compare health insurance",
    "independent health insurance agent",
    "health insurance broker",
    "health insurance agent near me",
    "self pay health insurance",
]

ACA_INTENT = [
    "aca health plan",
    "obamacare",
    "marketplace coverage",
    "healthcare.gov help",
    "silver plan",
    "gold plan",
    "aca subsidy",
]

AFFLUENT_TRIGGERS = [
    "self employed professional insurance",
    "health insurance for business owners",
    "executive coverage",
    "private ppo for family",
]

LIFE_EVENTS = [
    "lost coverage",
    "turning 26",
    "cobra is expensive",
    "cobra too expensive",
    "cobra alternative",
    "laid off need health insurance",
    "between jobs health insurance",
    "new job benefits",
    "moving states need new insurance",
    "early retirement health coverage",
    "self employed health insurance",
    "small business health plan",
    "student health plan",
]

BASE_KEYWORDS = list(
    set(PRIVATE_INTENT + GENERAL_INTENT + ACA_INTENT + AFFLUENT_TRIGGERS + LIFE_EVENTS)
)


def build_geo_keywords() -> list[str]:
    geo: list[str] = []
    for abbr in LICENSED_STATES:
        name = STATE_FULL_NAMES.get(abbr, abbr)
        for keyword in PRIVATE_INTENT + GENERAL_INTENT + ACA_INTENT:
            geo.extend([f"{keyword} in {name}", f"{keyword} {abbr}", f"{name} {keyword}"])
    return list(set(geo))


EXPANDED_KEYWORDS = list(set(BASE_KEYWORDS + build_geo_keywords()))
