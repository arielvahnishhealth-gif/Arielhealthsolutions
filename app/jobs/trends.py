try:
    from pytrends.request import TrendReq
except ImportError:  # pragma: no cover - optional integration
    TrendReq = None


def run_once() -> None:
    if TrendReq is None:
        return
    try:
        pytrends = TrendReq(hl="en-US", tz=360)
        pytrends.build_payload(
            kw_list=["health insurance", "private ppo", "ACA"],
            timeframe="now 7-d",
            geo="US",
        )
        pytrends.interest_over_time()
    except Exception:
        pass
