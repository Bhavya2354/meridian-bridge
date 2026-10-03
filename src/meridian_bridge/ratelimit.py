from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucket:
    def __init__(
        self,
        rate_per_minute: int,
        *,
        burst: int | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        self.rate_per_second = max(rate_per_minute, 1) / 60.0
        self.capacity = float(burst if burst is not None else max(1, rate_per_minute // 6))
        self._tokens = self.capacity
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()

    @property
    def available(self) -> float:
        return self._tokens

    async def acquire(self, tokens: float = 1.0) -> float:
        async with self._lock:
            waited = 0.0
            while True:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self.rate_per_second
                waited += delay
                await self._sleep(delay)

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._updated)
        self._updated = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_second)


@dataclass
class QuotaState:
    total: int | None = None
    remaining: int | None = None
    used_by_last_call: int | None = None
    retry_after: float | None = None
    observed_at: float | None = None
    throttled_calls: int = 0
    waited_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "plan_limit": self.total,
            "remaining": self.remaining,
            "used_by_last_call": self.used_by_last_call,
            "last_retry_after": self.retry_after,
            "throttled_calls": self.throttled_calls,
            "waited_seconds": round(self.waited_seconds, 3),
        }


class AdaptiveThrottle:
    def __init__(
        self,
        rate_per_minute: int,
        *,
        max_delay: float = 30.0,
        base_delay: float = 0.5,
        low_water_ratio: float = 0.1,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.bucket = TokenBucket(rate_per_minute, clock=clock, sleep=sleep)
        self.state = QuotaState()
        self.max_delay = max_delay
        self.base_delay = base_delay
        self.low_water_ratio = low_water_ratio
        self._sleep = sleep
        self._clock = clock
        self._rng = rng

    async def before_request(self) -> float:
        waited = await self.bucket.acquire()
        pace = self.pace_delay()
        if pace > 0:
            waited += pace
            await self._sleep(pace)
        self.state.waited_seconds += waited
        return waited

    def pace_delay(self) -> float:
        total, remaining = self.state.total, self.state.remaining
        if not total or remaining is None:
            return 0.0
        low_water = max(5, int(total * self.low_water_ratio))
        if remaining >= low_water:
            return 0.0
        if remaining <= 0:
            return self.max_delay
        return min(self.max_delay, self.base_delay * (low_water / remaining))

    def observe(self, headers: Mapping[str, str]) -> None:
        self.state.total = _header_int(headers, "x-ratelimit-total") or self.state.total
        remaining = _header_int(headers, "x-ratelimit-remaining")
        if remaining is not None:
            self.state.remaining = remaining
        used = _header_int(headers, "x-ratelimit-used-currentrequest")
        if used is not None:
            self.state.used_by_last_call = used
        self.state.observed_at = self._clock()

    def register_throttle(self, retry_after: float | None) -> None:
        self.state.throttled_calls += 1
        self.state.retry_after = retry_after
        self.state.remaining = 0

    def backoff_delay(self, attempt: int, retry_after: float | None = None) -> float:
        exponential = min(self.max_delay, self.base_delay * (2**attempt))
        jittered = exponential * (0.5 + 0.5 * self._rng())
        if retry_after is not None:
            return min(self.max_delay, max(retry_after, jittered))
        return jittered

    async def sleep_for_retry(self, attempt: int, retry_after: float | None = None) -> float:
        delay = self.backoff_delay(attempt, retry_after)
        self.state.waited_seconds += delay
        await self._sleep(delay)
        if self.state.remaining == 0:
            self.state.remaining = None
        return delay

    def snapshot(self) -> dict:
        return {
            "pace_rate_per_minute": round(self.bucket.rate_per_second * 60),
            "tokens_available": round(self.bucket.available, 2),
            **self.state.as_dict(),
        }


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name) or headers.get(name.title())
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None
