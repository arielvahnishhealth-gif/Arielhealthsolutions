"""Smoke-test the Docker/FastAPI stack logic without Postgres, Redis, or paid APIs.

Run after installing requirements:

    DATABASE_URL=sqlite:///data/smoke_fastapi.sqlite3 python scripts/smoke_fastapi_stack.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///data/smoke_fastapi.sqlite3")

from app.db import init_db  # noqa: E402
from app.jobs.score import geo_boost, process_manual_lead  # noqa: E402
from app.schema import QualifyIn  # noqa: E402
from app.services.keywords import EXPANDED_KEYWORDS  # noqa: E402
from app.services.qualify import create_or_update  # noqa: E402


def main() -> None:
    Path("data").mkdir(exist_ok=True)
    init_db()

    lead = process_manual_lead(
        {
            "url": "https://example.com/lead/private-ppo-texas",
            "message": "Looking for private PPO in Texas. COBRA is expensive.",
            "name": "Smoke Prospect",
            "keywords": ["private ppo", "cobra is expensive"],
        }
    )
    assert lead["id"], "lead id should be created"
    assert lead["score"] >= 3.0, "private PPO Texas lead should score as high intent"
    assert geo_boost("Need coverage in TX") == 2.0, "licensed state abbreviation should boost"
    assert any("private ppo" in keyword for keyword in EXPANDED_KEYWORDS)

    qualification = create_or_update(
        int(lead["id"]),
        QualifyIn(
            name="Smoke Prospect",
            zip="78701",
            primary={"dob": "1988-01-01", "height_in": 70, "weight_lb": 180, "smoker": False},
            income="$95,000",
            start_date="next month",
            carrier="Current Carrier",
            rate="$650",
            notes="Smoke test qualification",
        ),
    )
    assert qualification["lead_id"] == lead["id"]
    print({"ok": True, "lead": lead, "qualification": qualification})


if __name__ == "__main__":
    main()
