from __future__ import annotations

import httpx
import pytest

from meridian_bridge.auth import ApiKeyAuth, OAuth2Auth
from meridian_bridge.client import FreshdeskClient
from meridian_bridge.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
    ValidationError,
)
from tests.conftest import make_client, ticket_payload


async def test_api_key_auth_sends_basic_header(settings):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"id": 5, "contact": {"name": "Ada", "email": "ada@acme.io"}})

    async with make_client(settings, handler) as client:
        identity = await client.verify_credentials()

    assert seen[0].startswith("Basic ")
    assert identity["agent_id"] == 5
    assert identity["auth_mode"] == "api_key"


async def test_oauth_refreshes_token_and_caches_it(settings):
    token_calls = 0

    def token_handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        token_calls += 1
        return httpx.Response(200, json={"access_token": f"tok-{token_calls}", "expires_in": 3600})

    auth = OAuth2Auth(
        token_url="https://auth.example.com/token",
        client_id="cid",
        client_secret="secret",
        refresh_token="refresh",
        http=httpx.AsyncClient(transport=httpx.MockTransport(token_handler)),
    )

    assert (await auth.headers())["Authorization"] == "Bearer tok-1"
    assert (await auth.headers())["Authorization"] == "Bearer tok-1"
    await auth.invalidate()
    assert (await auth.headers())["Authorization"] == "Bearer tok-2"
    assert token_calls == 2
    await auth.aclose()


async def test_expired_credential_triggers_one_reauthorization(settings):
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, json={"message": "expired"})

    async with make_client(settings, handler) as client:
        with pytest.raises(AuthenticationError):
            await client.get("/tickets/1")

    assert len(attempts) == 2


async def test_retries_rate_limit_and_honors_retry_after(settings, clock, sleeper):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3", "x-ratelimit-total": "1000"})
        return httpx.Response(200, json=[ticket_payload()], headers={"x-ratelimit-remaining": "812"})

    async with make_client(settings, handler, clock=clock, sleeper=sleeper) as client:
        response = await client.request("GET", "/tickets")

    assert calls == 2
    assert response.attempts == 2
    assert sleeper.delays == [3.0]
    assert client.stats.rate_limited == 1
    assert client.throttle.state.remaining == 812


async def test_gives_up_after_max_retries(settings, clock, sleeper):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    async with make_client(settings, handler, clock=clock, sleeper=sleeper) as client:
        with pytest.raises(RateLimitError) as excinfo:
            await client.get("/tickets")

    assert excinfo.value.retry_after == 1.0
    assert client.stats.requests == settings.max_retries + 1


async def test_server_errors_retry_only_for_safe_methods(settings, clock, sleeper):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(503, json={"message": "unavailable"})

    async with make_client(settings, handler, clock=clock, sleeper=sleeper) as client:
        with pytest.raises(UpstreamError):
            await client.post("/tickets/1/notes", json={"body": "x"})
        assert calls == ["POST"]

        with pytest.raises(UpstreamError):
            await client.get("/tickets")
        assert calls.count("GET") == settings.max_retries + 1


async def test_network_failures_retry_then_surface(settings, clock, sleeper):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectTimeout("boom", request=request)
        return httpx.Response(200, json=[])

    async with make_client(settings, handler, clock=clock, sleeper=sleeper) as client:
        assert await client.get("/tickets") == []

    assert client.stats.retries == 2


async def test_error_status_mapping(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        code = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(code, json={"message": "nope"})

    async with make_client(settings, handler) as client:
        with pytest.raises(NotFoundError):
            await client.get("/tickets/404")
        with pytest.raises(ValidationError):
            await client.get("/tickets/400")


async def test_pagination_follows_link_header_within_budget(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        body = [ticket_payload(ticket_id=page * 100 + index) for index in range(100)]
        headers = {"link": f'<{settings.base_url}/tickets?page={page + 1}>; rel="next"'} if page < 9 else {}
        return httpx.Response(200, json=body, headers=headers)

    async with make_client(settings, handler) as client:
        rows, truncated = await client.paginate("/tickets")

    assert len(rows) == settings.max_pages_per_call * 100
    assert truncated is True


async def test_pagination_stops_on_short_page(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[ticket_payload(ticket_id=1)])

    async with make_client(settings, handler) as client:
        rows, truncated = await client.paginate("/tickets")

    assert len(rows) == 1
    assert truncated is False


async def test_client_builds_base_url_from_bare_domain():
    from meridian_bridge.config import Settings

    assert Settings(domain="acme", api_key="k").base_url == "https://acme.freshdesk.com/api/v2"
    assert Settings(domain="support.acme.com", api_key="k").base_url == "https://support.acme.com/api/v2"


async def test_default_client_construction_uses_settings(settings):
    client = FreshdeskClient(settings, ApiKeyAuth("key"))
    try:
        assert client.diagnostics()["quota"]["pace_rate_per_minute"] == settings.requests_per_minute
    finally:
        await client.aclose()
