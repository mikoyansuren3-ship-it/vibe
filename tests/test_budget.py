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


def test_per_second_builds_bucket_from_a_rate():
    b = RequestBudget.per_second(10.0)
    assert abs(b.rate - 10.0) < 1e-9
    assert b.capacity == 10  # default burst ~= 1 s of steady-state capacity
    b2 = RequestBudget.per_second(8.0, burst=20)
    assert abs(b2.rate - 8.0) < 1e-9
    assert b2.capacity == 20


async def test_acquire_releases_the_lock_while_it_sleeps():
    """A blocked acquire must NOT hold the bucket lock across its sleep — otherwise one
    waiter freezes every other caller (including an urgent poll) for the whole refill
    window. We probe the lock state from inside the sleep to prove it was released."""
    clk = FakeClock()
    b = RequestBudget(SECONDS_PER_DAY, burst=1, time_fn=clk.time, sleep_fn=clk.sleep)
    locked_during_sleep: list[bool] = []
    real_sleep = clk.sleep

    async def spy_sleep(seconds):
        locked_during_sleep.append(b._lock.locked())
        await real_sleep(seconds)

    b._sleep = spy_sleep
    await b.acquire()  # drain the single burst token
    await b.acquire()  # must wait for a refill; the lock must be free while it sleeps
    assert locked_during_sleep == [False]


async def test_kalshi_read_limiter_paces_gets_not_writes(monkeypatch):
    """The client-side limiter throttles GET reads (the market/orderbook fan-out) so
    parallel polling stays under the exchange's read tier — but order writes bypass it,
    so pacing never adds latency to the trade path. Patched at the httpx layer so the real
    retry/limiter wiring runs (the token is spent inside request_with_retry now)."""
    from wc_kalshi.ingestion.kalshi.client import KalshiClient

    class _Signer:  # non-None so writes are permitted; headers_for_url is never hit here
        def headers_for_url(self, method, url):
            return {}

    class _Resp:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    async def fake_request(method, url, **kwargs):
        return _Resp()

    limiter = RequestBudget.per_second(10.0, burst=50)
    client = KalshiClient("https://x", signer=_Signer(), read_limiter=limiter)
    monkeypatch.setattr(client._client, "request", fake_request)

    await client.get_orderbook("T1")
    await client.get_market("T2")
    assert limiter.granted == 2  # each GET read consumed exactly one token

    await client.create_order({"ticker": "T", "action": "buy", "count": 1})
    await client.cancel_order("oid")
    assert limiter.granted == 2  # POST/DELETE writes bypass the limiter — no token, no wait

    await client.aclose()


async def test_provider_consumes_budget_per_request(monkeypatch):
    """Each provider _get should consume exactly one token for a single (un-retried) wire
    request. Patched at the httpx layer so the per-attempt acquire in request_with_retry
    actually runs."""
    from wc_kalshi.ingestion.football.apifootball import APIFootballProvider

    b = RequestBudget(SECONDS_PER_DAY, burst=10)
    prov = APIFootballProvider(api_key="x", budget=b)

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": []}

    async def fake_request(method, url, **kwargs):
        return _Resp()

    monkeypatch.setattr(prov._client, "request", fake_request)
    before = b.available
    await prov._get("/fixtures", {"live": "all"})
    assert b.granted == 1  # exactly one token consumed for the request
    assert b.available <= before  # never more tokens than before the call
    await prov.aclose()
