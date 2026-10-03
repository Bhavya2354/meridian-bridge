from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from meridian_bridge.auth import AuthProvider, build_auth
from meridian_bridge.config import Settings
from meridian_bridge.errors import (
    AuthenticationError,
    AuthorizationError,
    MeridianError,
    NotFoundError,
    RateLimitError,
    UpstreamError,
    ValidationError,
)
from meridian_bridge.ratelimit import AdaptiveThrottle, parse_retry_after

logger = logging.getLogger("meridian_bridge.client")

SAFE_METHODS = {"GET", "HEAD"}
NEXT_LINK = re.compile(r'<(?P<url>[^>]+)>\s*;\s*rel="next"')
USER_AGENT = "meridian-bridge/1.0 (+mcp-connector)"


@dataclass
class ApiResponse:
    status: int
    data: Any
    headers: Mapping[str, str]
    attempts: int = 1

    @property
    def next_page(self) -> int | None:
        link = self.headers.get("link") or self.headers.get("Link")
        if not link:
            return None
        match = NEXT_LINK.search(link)
        if not match:
            return None
        page = httpx.URL(match.group("url")).params.get("page")
        return int(page) if page and page.isdigit() else None


@dataclass
class ClientStats:
    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    errors: int = 0

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "rate_limited": self.rate_limited,
            "errors": self.errors,
        }


class FreshdeskClient:
    def __init__(
        self,
        settings: Settings,
        auth: AuthProvider | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        throttle: AdaptiveThrottle | None = None,
    ) -> None:
        self.settings = settings
        self.auth = auth or build_auth(settings)
        self.stats = ClientStats()
        self.throttle = throttle or AdaptiveThrottle(
            settings.requests_per_minute,
            base_delay=settings.retry_base_delay,
            max_delay=settings.retry_max_delay,
        )
        self._http = http or httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )

    async def __aenter__(self) -> "FreshdeskClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self.auth.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> ApiResponse:
        method = method.upper()
        attempt = 0
        reauthorized = False
        while True:
            await self.throttle.before_request()
            self.stats.requests += 1
            try:
                response = await self._http.request(
                    method,
                    path,
                    params=_compact(params),
                    json=json,
                    headers=await self.auth.headers(),
                )
            except httpx.HTTPError as exc:
                if attempt >= self.settings.max_retries:
                    self.stats.errors += 1
                    raise UpstreamError(f"network failure calling Freshdesk: {exc}") from exc
                attempt += 1
                self.stats.retries += 1
                await self.throttle.sleep_for_retry(attempt)
                continue

            self.throttle.observe(response.headers)
            status = response.status_code

            if status == 429:
                retry_after = parse_retry_after(response.headers)
                self.stats.rate_limited += 1
                self.throttle.register_throttle(retry_after)
                if attempt >= self.settings.max_retries:
                    self.stats.errors += 1
                    raise RateLimitError(
                        "Freshdesk rate limit exhausted after retries",
                        retry_after=retry_after,
                        details=self.throttle.snapshot(),
                    )
                attempt += 1
                self.stats.retries += 1
                delay = await self.throttle.sleep_for_retry(attempt, retry_after)
                logger.warning("rate limited by Freshdesk; retrying in %.2fs", delay)
                continue

            if status == 401 and not reauthorized:
                reauthorized = True
                await self.auth.invalidate()
                continue

            if status >= 500 and method in SAFE_METHODS and attempt < self.settings.max_retries:
                attempt += 1
                self.stats.retries += 1
                await self.throttle.sleep_for_retry(attempt)
                continue

            if status >= 400:
                self.stats.errors += 1
                raise self._map_error(status, response)

            return ApiResponse(
                status=status,
                data=_decode(response),
                headers=response.headers,
                attempts=attempt + 1,
            )

    async def get(self, path: str, *, params: dict | None = None) -> Any:
        return (await self.request("GET", path, params=params)).data

    async def post(self, path: str, *, json: dict) -> Any:
        return (await self.request("POST", path, json=json)).data

    async def paginate(
        self,
        path: str,
        *,
        params: dict | None = None,
        per_page: int | None = None,
        max_pages: int | None = None,
        max_items: int | None = None,
    ) -> tuple[list[dict], bool]:
        page_size = min(per_page or self.settings.max_page_size, self.settings.max_page_size)
        budget = max_pages or self.settings.max_pages_per_call
        page = int((params or {}).get("page") or 1)
        items: list[dict] = []
        truncated = False

        for _ in range(budget):
            response = await self.request(
                "GET", path, params={**(params or {}), "page": page, "per_page": page_size}
            )
            batch = response.data if isinstance(response.data, list) else response.data.get("results", [])
            items.extend(batch)
            if max_items and len(items) >= max_items:
                items = items[:max_items]
                truncated = response.next_page is not None or len(batch) == page_size
                break
            next_page = response.next_page or (page + 1 if len(batch) == page_size else None)
            if not next_page:
                break
            page = next_page
        else:
            truncated = True

        return items, truncated

    async def verify_credentials(self) -> dict:
        agent = await self.get("/agents/me")
        return {
            "auth_mode": self.auth.mode,
            "account": self.settings.domain,
            "agent_id": agent.get("id"),
            "agent_name": (agent.get("contact") or {}).get("name"),
            "agent_email": (agent.get("contact") or {}).get("email"),
            "scopes": agent.get("ticket_scope"),
        }

    def diagnostics(self) -> dict:
        return {"http": self.stats.as_dict(), "quota": self.throttle.snapshot()}

    def _map_error(self, status: int, response: httpx.Response) -> MeridianError:
        body = _decode(response)
        details = {"status": status, "body": body if isinstance(body, (dict, list)) else str(body)[:500]}
        if status in (401, 403):
            message = "Freshdesk rejected the credential" if status == 401 else "credential lacks permission for this resource"
            error = AuthenticationError if status == 401 else AuthorizationError
            return error(message, details=details)
        if status == 404:
            return NotFoundError("Freshdesk resource not found", details=details)
        if status in (400, 406, 409, 422):
            return ValidationError("Freshdesk rejected the request parameters", details=details)
        return UpstreamError(f"Freshdesk returned HTTP {status}", details=details)


def _decode(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _compact(params: dict | None) -> dict | None:
    if not params:
        return None
    return {key: value for key, value in params.items() if value is not None}
