from __future__ import annotations

from meridian_bridge.ratelimit import AdaptiveThrottle, TokenBucket, parse_retry_after


async def test_bucket_paces_requests_once_burst_is_spent(clock, sleeper):
    bucket = TokenBucket(60, clock=clock, sleep=sleeper)

    for _ in range(int(bucket.capacity)):
        assert await bucket.acquire() == 0.0

    waited = await bucket.acquire()
    assert waited == 1.0
    assert sleeper.delays == [1.0]


async def test_bucket_refills_over_time(clock, sleeper):
    bucket = TokenBucket(60, clock=clock, sleep=sleeper)
    while bucket.available >= 1:
        await bucket.acquire()

    clock.advance(5)
    assert await bucket.acquire() == 0.0


async def test_throttle_adopts_quota_headers(clock, sleeper):
    throttle = AdaptiveThrottle(600, clock=clock, sleep=sleeper)
    throttle.observe({"x-ratelimit-total": "1000", "x-ratelimit-remaining": "940", "x-ratelimit-used-currentrequest": "1"})

    assert throttle.state.remaining == 940
    assert throttle.pace_delay() == 0.0
    assert throttle.snapshot()["plan_limit"] == 1000


async def test_throttle_slows_down_near_quota_floor(clock, sleeper):
    throttle = AdaptiveThrottle(600, base_delay=1.0, max_delay=10.0, clock=clock, sleep=sleeper)
    throttle.observe({"x-ratelimit-total": "1000", "x-ratelimit-remaining": "20"})

    assert throttle.pace_delay() == 5.0
    await throttle.before_request()
    assert sleeper.total == 5.0


async def test_throttle_stops_hard_when_quota_is_gone(clock, sleeper):
    throttle = AdaptiveThrottle(600, max_delay=30.0, clock=clock, sleep=sleeper)
    throttle.register_throttle(retry_after=12.0)

    assert throttle.state.throttled_calls == 1
    assert throttle.pace_delay() == 0.0
    throttle.observe({"x-ratelimit-total": "1000"})
    assert throttle.pace_delay() == 30.0


async def test_backoff_grows_and_respects_retry_after(clock, sleeper):
    throttle = AdaptiveThrottle(600, base_delay=0.5, max_delay=20.0, clock=clock, sleep=sleeper, rng=lambda: 1.0)

    assert throttle.backoff_delay(1) == 1.0
    assert throttle.backoff_delay(3) == 4.0
    assert throttle.backoff_delay(1, retry_after=9.0) == 9.0
    assert throttle.backoff_delay(9) == 20.0


def test_parse_retry_after():
    assert parse_retry_after({"Retry-After": "7"}) == 7.0
    assert parse_retry_after({"retry-after": "junk"}) is None
    assert parse_retry_after({}) is None
