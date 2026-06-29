import os
import time

import requests
import schedule


API = os.getenv("SCHEDULER_API_BASE", "http://api:8080")


def tick() -> None:
    try:
        requests.post(f"{API}/run/listeners", timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    schedule.every(10).minutes.do(tick)
    while True:
        schedule.run_pending()
        time.sleep(2)
