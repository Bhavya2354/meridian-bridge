from __future__ import annotations

import asyncio
import base64
import time
from typing import Protocol

import httpx

from meridian_bridge.config import Settings
from meridian_bridge.errors import AuthenticationError, ConfigurationError

TOKEN_EXPIRY_SKEW = 60.0


class AuthProvider(Protocol):
    mode: str

    async def headers(self) -> dict[str, str]: ...

    async def invalidate(self) -> None: ...

    async def aclose(self) -> None: ...


class ApiKeyAuth:
    mode = "api_key"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ConfigurationError("api key must not be empty")
        token = base64.b64encode(f"{api_key}:X".encode()).decode()
        self._header = {"Authorization": f"Basic {token}"}

    async def headers(self) -> dict[str, str]:
        return dict(self._header)

    async def invalidate(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class OAuth2Auth:
    mode = "oauth"

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        http: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
        clock=time.monotonic,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = http
        self._owns_http = http is None
        self._timeout = timeout
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._token()}"}

    async def invalidate(self) -> None:
        async with self._lock:
            self._access_token = None
            self._expires_at = 0.0

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _token(self) -> str:
        if self._access_token and self._clock() < self._expires_at:
            return self._access_token
        async with self._lock:
            if self._access_token and self._clock() < self._expires_at:
                return self._access_token
            payload = await self._request_token()
            token = payload.get("access_token")
            if not token:
                raise AuthenticationError("token endpoint returned no access_token", details=payload)
            expires_in = float(payload.get("expires_in", 3600))
            if payload.get("refresh_token"):
                self._refresh_token = payload["refresh_token"]
            self._access_token = token
            self._expires_at = self._clock() + max(expires_in - TOKEN_EXPIRY_SKEW, 30.0)
            return token

    async def _request_token(self) -> dict:
        client = self._client()
        try:
            response = await client.post(
                self._token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise AuthenticationError(f"token endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise AuthenticationError(
                f"token refresh failed with HTTP {response.status_code}",
                details={"body": response.text[:500]},
            )
        return response.json()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient()
            self._owns_http = True
        return self._http


def build_auth(settings: Settings, *, http: httpx.AsyncClient | None = None) -> AuthProvider:
    if settings.auth_mode == "api_key":
        return ApiKeyAuth(settings.api_key or "")
    return OAuth2Auth(
        token_url=settings.oauth_token_url,
        client_id=settings.oauth_client_id or "",
        client_secret=settings.oauth_client_secret or "",
        refresh_token=settings.oauth_refresh_token or "",
        http=http,
        timeout=settings.request_timeout,
    )
