from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from meridian_bridge.errors import ConfigurationError

TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_TOKEN_URL = "https://api.freshworks.com/crm/oauth/token"


def load_dotenv(path: str | Path = ".env") -> None:
    file = Path(path)
    if not file.is_file():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


@dataclass(frozen=True)
class Settings:
    domain: str
    base_url_override: str | None = None
    auth_mode: str = "api_key"
    api_key: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_refresh_token: str | None = None
    oauth_token_url: str = DEFAULT_TOKEN_URL
    requests_per_minute: int = 50
    max_retries: int = 4
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    request_timeout: float = 20.0
    max_page_size: int = 100
    max_pages_per_call: int = 5
    allow_writes: bool = False
    redact_pii: bool = True

    @property
    def base_url(self) -> str:
        if self.base_url_override:
            return self.base_url_override.rstrip("/")
        host = self.domain if "." in self.domain else f"{self.domain}.freshdesk.com"
        return f"https://{host.rstrip('/')}/api/v2"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = env if env is not None else os.environ
        settings = cls(
            domain=env.get("FRESHDESK_DOMAIN", "").strip(),
            base_url_override=_clean(env.get("MERIDIAN_BASE_URL")),
            auth_mode=env.get("FRESHDESK_AUTH_MODE", "api_key").strip().lower(),
            api_key=_clean(env.get("FRESHDESK_API_KEY")),
            oauth_client_id=_clean(env.get("FRESHDESK_OAUTH_CLIENT_ID")),
            oauth_client_secret=_clean(env.get("FRESHDESK_OAUTH_CLIENT_SECRET")),
            oauth_refresh_token=_clean(env.get("FRESHDESK_OAUTH_REFRESH_TOKEN")),
            oauth_token_url=env.get("FRESHDESK_OAUTH_TOKEN_URL", DEFAULT_TOKEN_URL).strip(),
            requests_per_minute=_int(env, "MERIDIAN_REQUESTS_PER_MINUTE", 50),
            max_retries=_int(env, "MERIDIAN_MAX_RETRIES", 4),
            retry_base_delay=_float(env, "MERIDIAN_RETRY_BASE_DELAY", 0.5),
            retry_max_delay=_float(env, "MERIDIAN_RETRY_MAX_DELAY", 30.0),
            request_timeout=_float(env, "MERIDIAN_REQUEST_TIMEOUT", 20.0),
            max_page_size=min(_int(env, "MERIDIAN_MAX_PAGE_SIZE", 100), 100),
            max_pages_per_call=_int(env, "MERIDIAN_MAX_PAGES_PER_CALL", 5),
            allow_writes=_bool(env, "MERIDIAN_ALLOW_WRITES", False),
            redact_pii=_bool(env, "MERIDIAN_REDACT_PII", True),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.domain:
            raise ConfigurationError("FRESHDESK_DOMAIN is required (e.g. acme or acme.freshdesk.com)")
        if self.auth_mode not in {"api_key", "oauth"}:
            raise ConfigurationError("FRESHDESK_AUTH_MODE must be 'api_key' or 'oauth'")
        if self.auth_mode == "api_key" and not self.api_key:
            raise ConfigurationError("FRESHDESK_API_KEY is required when FRESHDESK_AUTH_MODE=api_key")
        if self.auth_mode == "oauth":
            missing = [
                name
                for name, value in (
                    ("FRESHDESK_OAUTH_CLIENT_ID", self.oauth_client_id),
                    ("FRESHDESK_OAUTH_CLIENT_SECRET", self.oauth_client_secret),
                    ("FRESHDESK_OAUTH_REFRESH_TOKEN", self.oauth_refresh_token),
                )
                if not value
            ]
            if missing:
                raise ConfigurationError(f"missing OAuth settings: {', '.join(missing)}")
        if self.base_url_override:
            self._validate_base_url(self.base_url_override)
        if self.auth_mode == "oauth" and not self.oauth_token_url.startswith("https://"):
            raise ConfigurationError("FRESHDESK_OAUTH_TOKEN_URL must be an https:// URL")
        for name, value, floor in (
            ("MERIDIAN_REQUESTS_PER_MINUTE", self.requests_per_minute, 1),
            ("MERIDIAN_MAX_PAGE_SIZE", self.max_page_size, 1),
            ("MERIDIAN_MAX_PAGES_PER_CALL", self.max_pages_per_call, 1),
            ("MERIDIAN_REQUEST_TIMEOUT", self.request_timeout, 1),
            ("MERIDIAN_RETRY_BASE_DELAY", self.retry_base_delay, 0),
            ("MERIDIAN_MAX_RETRIES", self.max_retries, 0),
        ):
            if value < floor:
                raise ConfigurationError(f"{name} must be >= {floor}")
        if self.retry_max_delay < self.retry_base_delay:
            raise ConfigurationError("MERIDIAN_RETRY_MAX_DELAY must be >= MERIDIAN_RETRY_BASE_DELAY")

    @staticmethod
    def _validate_base_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigurationError("MERIDIAN_BASE_URL must be an http:// or https:// URL")
        if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
            raise ConfigurationError("MERIDIAN_BASE_URL may only use http:// for loopback hosts")

    def redacted(self) -> dict:
        return {
            "domain": self.domain,
            "base_url": self.base_url,
            "auth_mode": self.auth_mode,
            "credential_present": bool(self.api_key or self.oauth_refresh_token),
            "requests_per_minute": self.requests_per_minute,
            "max_pages_per_call": self.max_pages_per_call,
            "allow_writes": self.allow_writes,
            "redact_pii": self.redact_pii,
        }


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be an integer") from exc


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{key} must be a number") from exc


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUE_VALUES
