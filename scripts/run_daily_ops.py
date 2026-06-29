from __future__ import annotations

import json
import os
import urllib.request


BASE_URL = os.getenv("LEAD_BOT_URL", "http://127.0.0.1:8080")
API_KEY = os.getenv("PRINCIPAL_API_KEY", "principal-dev-key")


def post(path: str) -> dict:
    request = urllib.request.Request(
        BASE_URL + path,
        data=b"{}",
        headers={"Content-Type": "application/json", "X-API-Key": API_KEY, "X-Actor": "daily-ops"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    print(json.dumps(post("/api/automation/run"), indent=2))
