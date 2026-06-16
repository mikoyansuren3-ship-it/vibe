"""Shared request budgeter (token bucket) sized to the daily quota."""

from wc_kalshi.ingestion.budget import SECONDS_PER_DAY, RequestBudget


class FakeClock:
    """Controllable monotonic clock; sleeping just advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def time(self):
        return self.t

    async def sleep(self, seconds):
        self.slept += seconds
        self.t += seconds


def test_rate_is_daily_budget_per_day():
    b = RequestBudget(75_000)
    assert abs(b.rate - 75_000 / SECONDS_PER_DAY) < 1e-12
    # burst defaults to ~1 minute of steady-state requests
    assert b.capacity == round(b.rate * 60)


def test_burst_then_throttle_blocks_until_refill():
    clk = FakeClock()
    # 86,400/day => exactly 1 token/sec, burst capacity 60.
    b = RequestBudget(SECONDS_PER_DAY, time_fn=clk.time, sleep_fn=clk.sleep)
    assert b.capacity == 60

    import asyncio

    async def drive():
        # drain the full burst without advancing time
        for _ in range(60):
            await b.acquire()
        assert clk.slept == 0.0
        # the 61st must wait ~1 second (1 token/sec refill)
        await b.acquire()
        assert abs(clk.slept - 1.0) < 1e-9

    asyncio.run(drive())
    assert b.granted == 61


def test_try_acquire_is_nonblocking():
    clk = FakeClock()
    b = RequestBudget(SECONDS_PER_DAY, burst=2, time_fn=clk.time, sleep_fn=clk.sleep)
    assert b.try_acquire() is True
    assert b.try_acquire() is True
    assert b.try_acquire() is False  # bucket empty, no blocking
    clk.t += 5  # 5 seconds -> 5 tokens refill (capped at capacity 2)
    assert b.available == 2.0
    assert b.try_acquire() is True


async def test_provider_consumes_budget_per_request(monkeypatch):
    """Each provider _get should consume exactly one token."""
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    b = RequestBudget(SECONDS_PER_DAY, burst=10)
    prov = APIFootballProvider(api_key="x", budget=b)

    async def fake_request(*args, **kwargs):
        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"response": []}

        return R()

    monkeypatch.setattr("wc_kalshi.ingestion.football.apifootball.request_with_retry", fake_request)
    before = b.available
    await prov._get("/fixtures", {"live": "all"})
    assert b.granted == 1  # exactly one token consumed for the request
    assert b.available <= before  # never more tokens than before the call
    await prov.aclose()
