from __future__ import annotations

import httpx
import pytest

from meridian_bridge.auth import ApiKeyAuth
from meridian_bridge.client import FreshdeskClient
from meridian_bridge.config import Settings
from meridian_bridge.ratelimit import AdaptiveThrottle
from meridian_bridge.tickets import TicketService


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleeper:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.clock.advance(seconds)

    @property
    def total(self) -> float:
        return sum(self.delays)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sleeper(clock: FakeClock) -> RecordingSleeper:
    return RecordingSleeper(clock)


@pytest.fixture
def settings() -> Settings:
    return Settings(domain="acme", api_key="test-key", requests_per_minute=600, max_retries=3)


def make_client(settings: Settings, handler, *, clock=None, sleeper=None) -> FreshdeskClient:
    throttle = AdaptiveThrottle(
        settings.requests_per_minute,
        base_delay=0.1,
        max_delay=5.0,
        clock=clock or (lambda: 0.0),
        sleep=sleeper or _noop_sleep,
        rng=lambda: 1.0,
    )
    http = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    return FreshdeskClient(settings, ApiKeyAuth(settings.api_key or "k"), http=http, throttle=throttle)


def make_service(settings: Settings, handler, **kwargs) -> TicketService:
    return TicketService(make_client(settings, handler, **kwargs))


async def _noop_sleep(_seconds: float) -> None:
    return None


def ticket_payload(ticket_id: int = 1, **overrides) -> dict:
    payload = {
        "id": ticket_id,
        "subject": f"Order #{ticket_id} never arrived",
        "status": 2,
        "priority": 3,
        "source": 1,
        "requester_id": 900 + ticket_id,
        "email": "shopper@example.com",
        "responder_id": 11,
        "group_id": 7,
        "type": "Incident",
        "tags": ["delivery"],
        "created_at": "2026-09-01T10:00:00Z",
        "updated_at": "2026-09-02T10:00:00Z",
        "due_by": "2026-09-03T10:00:00Z",
        "fr_due_by": "2026-09-01T12:00:00Z",
        "is_escalated": False,
        "spam": False,
        "description": "<p>My parcel is late. Reach me at shopper@example.com</p>",
    }
    payload.update(overrides)
    return payload
