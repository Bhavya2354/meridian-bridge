from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

from meridian_bridge.config import DEFAULT_TOKEN_URL, Settings, load_dotenv
from meridian_bridge.errors import ConfigurationError

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / ".env.example"
SOURCE_FILES = sorted((ROOT / "src").rglob("*.py"))
ENV_NAME = re.compile(r"\"(FRESHDESK_[A-Z0-9_]+|MERIDIAN_[A-Z0-9_]+)\"")

API_KEY_ENV = {"FRESHDESK_DOMAIN": "acme", "FRESHDESK_API_KEY": "key-123"}
OAUTH_ENV = {
    "FRESHDESK_DOMAIN": "acme",
    "FRESHDESK_AUTH_MODE": "oauth",
    "FRESHDESK_OAUTH_CLIENT_ID": "cid",
    "FRESHDESK_OAUTH_CLIENT_SECRET": "secret",
    "FRESHDESK_OAUTH_REFRESH_TOKEN": "refresh",
}

ENV_SPEC: dict[str, tuple[str | None, object]] = {
    "FRESHDESK_DOMAIN": ("domain", None),
    "MERIDIAN_BASE_URL": ("base_url_override", None),
    "FRESHDESK_AUTH_MODE": ("auth_mode", "api_key"),
    "FRESHDESK_API_KEY": ("api_key", None),
    "FRESHDESK_OAUTH_CLIENT_ID": ("oauth_client_id", None),
    "FRESHDESK_OAUTH_CLIENT_SECRET": ("oauth_client_secret", None),
    "FRESHDESK_OAUTH_REFRESH_TOKEN": ("oauth_refresh_token", None),
    "FRESHDESK_OAUTH_TOKEN_URL": ("oauth_token_url", DEFAULT_TOKEN_URL),
    "MERIDIAN_REQUESTS_PER_MINUTE": ("requests_per_minute", 50),
    "MERIDIAN_MAX_RETRIES": ("max_retries", 4),
    "MERIDIAN_RETRY_BASE_DELAY": ("retry_base_delay", 0.5),
    "MERIDIAN_RETRY_MAX_DELAY": ("retry_max_delay", 30.0),
    "MERIDIAN_REQUEST_TIMEOUT": ("request_timeout", 20.0),
    "MERIDIAN_MAX_PAGE_SIZE": ("max_page_size", 100),
    "MERIDIAN_MAX_PAGES_PER_CALL": ("max_pages_per_call", 5),
    "MERIDIAN_ALLOW_WRITES": ("allow_writes", False),
    "MERIDIAN_REDACT_PII": ("redact_pii", True),
    "MERIDIAN_ENV_FILE": (None, ".env"),
    "MERIDIAN_TRANSPORT": (None, "stdio"),
    "MERIDIAN_LOG_LEVEL": (None, "INFO"),
}

OVERRIDES = {
    "FRESHDESK_DOMAIN": ("support.acme.com", "support.acme.com"),
    "MERIDIAN_BASE_URL": ("https://gateway.acme.com/freshdesk/api/v2", "https://gateway.acme.com/freshdesk/api/v2"),
    "FRESHDESK_AUTH_MODE": ("oauth", "oauth"),
    "FRESHDESK_API_KEY": ("  key-123  ", "key-123"),
    "FRESHDESK_OAUTH_CLIENT_ID": ("cid", "cid"),
    "FRESHDESK_OAUTH_CLIENT_SECRET": ("secret", "secret"),
    "FRESHDESK_OAUTH_REFRESH_TOKEN": ("refresh", "refresh"),
    "FRESHDESK_OAUTH_TOKEN_URL": ("https://api.eu.freshworks.com/token", "https://api.eu.freshworks.com/token"),
    "MERIDIAN_REQUESTS_PER_MINUTE": ("120", 120),
    "MERIDIAN_MAX_RETRIES": ("7", 7),
    "MERIDIAN_RETRY_BASE_DELAY": ("1.5", 1.5),
    "MERIDIAN_RETRY_MAX_DELAY": ("45", 45.0),
    "MERIDIAN_REQUEST_TIMEOUT": ("9.5", 9.5),
    "MERIDIAN_MAX_PAGE_SIZE": ("30", 30),
    "MERIDIAN_MAX_PAGES_PER_CALL": ("2", 2),
    "MERIDIAN_ALLOW_WRITES": ("true", True),
    "MERIDIAN_REDACT_PII": ("false", False),
}

SETTINGS_FIELDS = {field.name for field in dataclasses.fields(Settings)}


def documented_names() -> set[str]:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"\b(FRESHDESK_[A-Z0-9_]+|MERIDIAN_[A-Z0-9_]+)\b", text))


def source_names() -> set[str]:
    return {name for file in SOURCE_FILES for name in ENV_NAME.findall(file.read_text(encoding="utf-8"))}


def test_every_env_var_read_by_the_code_is_documented():
    assert source_names() - documented_names() == set()


def test_env_example_documents_nothing_the_code_ignores():
    assert documented_names() - set(ENV_SPEC) == set()


def test_every_settings_field_is_reachable_from_the_environment():
    mapped = {field for field, _ in ENV_SPEC.values() if field}
    assert SETTINGS_FIELDS == mapped


def test_spec_matches_the_names_the_code_actually_reads():
    runtime_only = {name for name, (field, _) in ENV_SPEC.items() if field is None}
    assert set(ENV_SPEC) - runtime_only <= source_names()
    assert runtime_only <= source_names()


def test_domain_is_required():
    with pytest.raises(ConfigurationError, match="FRESHDESK_DOMAIN"):
        Settings.from_env({"FRESHDESK_API_KEY": "key"})


def test_blank_domain_is_rejected():
    with pytest.raises(ConfigurationError, match="FRESHDESK_DOMAIN"):
        Settings.from_env({"FRESHDESK_DOMAIN": "   ", "FRESHDESK_API_KEY": "key"})


def test_api_key_is_required_in_api_key_mode():
    with pytest.raises(ConfigurationError, match="FRESHDESK_API_KEY"):
        Settings.from_env({"FRESHDESK_DOMAIN": "acme"})


@pytest.mark.parametrize(
    "missing",
    ["FRESHDESK_OAUTH_CLIENT_ID", "FRESHDESK_OAUTH_CLIENT_SECRET", "FRESHDESK_OAUTH_REFRESH_TOKEN"],
)
def test_each_oauth_credential_is_required_in_oauth_mode(missing):
    env = {key: value for key, value in OAUTH_ENV.items() if key != missing}
    with pytest.raises(ConfigurationError, match=missing):
        Settings.from_env(env)


def test_oauth_mode_does_not_need_an_api_key():
    settings = Settings.from_env(OAUTH_ENV)
    assert settings.auth_mode == "oauth"
    assert settings.api_key is None
    assert settings.oauth_token_url == DEFAULT_TOKEN_URL


def test_api_key_mode_does_not_need_oauth_credentials():
    settings = Settings.from_env(API_KEY_ENV)
    assert settings.auth_mode == "api_key"
    assert settings.oauth_refresh_token is None


def test_unknown_auth_mode_is_rejected():
    with pytest.raises(ConfigurationError, match="FRESHDESK_AUTH_MODE"):
        Settings.from_env({**API_KEY_ENV, "FRESHDESK_AUTH_MODE": "saml"})


def test_auth_mode_is_case_insensitive():
    assert Settings.from_env({**API_KEY_ENV, "FRESHDESK_AUTH_MODE": " API_KEY "}).auth_mode == "api_key"


def test_insecure_token_url_is_rejected():
    with pytest.raises(ConfigurationError, match="https"):
        Settings.from_env({**OAUTH_ENV, "FRESHDESK_OAUTH_TOKEN_URL": "http://auth.local/token"})


@pytest.mark.parametrize("name", [name for name, (field, _) in ENV_SPEC.items() if field])
def test_defaults_apply_when_only_required_vars_are_set(name):
    field, default = ENV_SPEC[name]
    env = {**API_KEY_ENV}
    if name in env:
        pytest.skip("required variable, covered by its own test")
    assert getattr(Settings.from_env(env), field) == default


@pytest.mark.parametrize("name", sorted(OVERRIDES))
def test_each_variable_is_parsed_into_its_setting(name):
    raw, expected = OVERRIDES[name]
    base = OAUTH_ENV if name.startswith("FRESHDESK_OAUTH") or name == "FRESHDESK_AUTH_MODE" else API_KEY_ENV
    settings = Settings.from_env({**base, name: raw})
    assert getattr(settings, ENV_SPEC[name][0]) == expected


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " True "])
def test_truthy_flag_spellings(raw):
    assert Settings.from_env({**API_KEY_ENV, "MERIDIAN_ALLOW_WRITES": raw}).allow_writes is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe", ""])
def test_falsy_and_unparseable_flag_spellings(raw):
    assert Settings.from_env({**API_KEY_ENV, "MERIDIAN_ALLOW_WRITES": raw}).allow_writes is False


def test_empty_flag_keeps_the_default_for_on_by_default_switches():
    assert Settings.from_env({**API_KEY_ENV, "MERIDIAN_REDACT_PII": ""}).redact_pii is True


@pytest.mark.parametrize(
    "name", ["MERIDIAN_REQUESTS_PER_MINUTE", "MERIDIAN_MAX_RETRIES", "MERIDIAN_MAX_PAGE_SIZE", "MERIDIAN_MAX_PAGES_PER_CALL"]
)
def test_non_numeric_integers_are_rejected(name):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({**API_KEY_ENV, name: "lots"})


@pytest.mark.parametrize("name", ["MERIDIAN_REQUEST_TIMEOUT", "MERIDIAN_RETRY_BASE_DELAY", "MERIDIAN_RETRY_MAX_DELAY"])
def test_non_numeric_floats_are_rejected(name):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({**API_KEY_ENV, name: "soon"})


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("MERIDIAN_REQUESTS_PER_MINUTE", "0"),
        ("MERIDIAN_MAX_RETRIES", "-1"),
        ("MERIDIAN_MAX_PAGE_SIZE", "0"),
        ("MERIDIAN_MAX_PAGES_PER_CALL", "0"),
        ("MERIDIAN_REQUEST_TIMEOUT", "0"),
        ("MERIDIAN_RETRY_BASE_DELAY", "-2"),
    ],
)
def test_out_of_range_values_are_rejected(name, raw):
    with pytest.raises(ConfigurationError, match=name):
        Settings.from_env({**API_KEY_ENV, name: raw})


def test_retry_window_must_not_be_inverted():
    with pytest.raises(ConfigurationError, match="MERIDIAN_RETRY_MAX_DELAY"):
        Settings.from_env({**API_KEY_ENV, "MERIDIAN_RETRY_BASE_DELAY": "10", "MERIDIAN_RETRY_MAX_DELAY": "2"})


def test_page_size_is_clamped_to_the_freshdesk_maximum():
    assert Settings.from_env({**API_KEY_ENV, "MERIDIAN_MAX_PAGE_SIZE": "5000"}).max_page_size == 100


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("acme", "https://acme.freshdesk.com/api/v2"),
        ("acme.freshdesk.com", "https://acme.freshdesk.com/api/v2"),
        ("support.acme.com", "https://support.acme.com/api/v2"),
        ("acme.freshdesk.com/", "https://acme.freshdesk.com/api/v2"),
    ],
)
def test_domain_forms_resolve_to_one_base_url(domain, expected):
    assert Settings.from_env({**API_KEY_ENV, "FRESHDESK_DOMAIN": domain}).base_url == expected


def test_base_url_override_replaces_the_derived_root():
    settings = Settings.from_env({**API_KEY_ENV, "MERIDIAN_BASE_URL": "https://gateway.acme.com/fd/api/v2/"})
    assert settings.base_url == "https://gateway.acme.com/fd/api/v2"


@pytest.mark.parametrize("value", ["http://127.0.0.1:8080/api/v2", "http://localhost:9000/api/v2"])
def test_plain_http_base_url_is_allowed_for_loopback(value):
    assert Settings.from_env({**API_KEY_ENV, "MERIDIAN_BASE_URL": value}).base_url == value


@pytest.mark.parametrize("value", ["http://freshdesk.com/api/v2", "ftp://acme.com", "not-a-url"])
def test_unsafe_base_url_overrides_are_rejected(value):
    with pytest.raises(ConfigurationError, match="MERIDIAN_BASE_URL"):
        Settings.from_env({**API_KEY_ENV, "MERIDIAN_BASE_URL": value})


@pytest.fixture
def isolated_environ():
    snapshot = dict(os.environ)
    try:
        yield os.environ
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def test_dotenv_file_populates_the_process_environment(tmp_path, isolated_environ):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "FRESHDESK_DOMAIN=acme",
                "FRESHDESK_API_KEY='quoted-key'",
                'MERIDIAN_REQUESTS_PER_MINUTE="25"',
                "MALFORMED LINE",
            ]
        ),
        encoding="utf-8",
    )
    for name in ("FRESHDESK_DOMAIN", "FRESHDESK_API_KEY", "MERIDIAN_REQUESTS_PER_MINUTE"):
        isolated_environ.pop(name, None)

    load_dotenv(env_file)
    settings = Settings.from_env()

    assert settings.domain == "acme"
    assert settings.api_key == "quoted-key"
    assert settings.requests_per_minute == 25


def test_real_environment_wins_over_the_dotenv_file(tmp_path, isolated_environ):
    env_file = tmp_path / ".env"
    env_file.write_text("FRESHDESK_DOMAIN=from-file\nFRESHDESK_API_KEY=from-file", encoding="utf-8")
    isolated_environ["FRESHDESK_DOMAIN"] = "from-shell"
    isolated_environ.pop("FRESHDESK_API_KEY", None)

    load_dotenv(env_file)

    assert isolated_environ["FRESHDESK_DOMAIN"] == "from-shell"
    assert Settings.from_env().domain == "from-shell"
    assert Settings.from_env().api_key == "from-file"


def test_missing_dotenv_file_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "absent.env")


@pytest.mark.parametrize("env", [API_KEY_ENV, OAUTH_ENV])
def test_redacted_config_contains_no_secret_values(env):
    settings = Settings.from_env(env)
    exposed = str(settings.redacted())

    for secret in ("key-123", "secret", "refresh", "cid"):
        assert secret not in exposed
    assert settings.redacted()["credential_present"] is True
