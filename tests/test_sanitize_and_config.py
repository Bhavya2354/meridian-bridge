from __future__ import annotations

import pytest

from meridian_bridge.config import Settings
from meridian_bridge.errors import ConfigurationError
from meridian_bridge.sanitize import clean_text, html_to_text, redact


def test_html_is_flattened_to_readable_text():
    html = "<style>p{}</style><p>Hello&nbsp;there</p><br><ul><li>one</li><li>two</li></ul>"
    assert html_to_text(html) == "Hello there\n\none\ntwo"


def test_redaction_masks_contact_details():
    text = redact("mail me at jane.doe@acme.io or call +1 415 555 0132, card 4111 1111 1111 1111")
    assert "jane.doe@acme.io" not in text
    assert "j*******@acme.io" in text
    assert "[redacted-phone]" in text
    assert "[redacted-card]" in text


def test_clean_text_truncates_long_bodies():
    cleaned = clean_text("<p>" + "a" * 500 + "</p>", limit=100)
    assert cleaned.endswith("[truncated at 100 characters]")
    assert len(cleaned) < 200


def test_clean_text_can_keep_pii_when_disabled():
    assert "jane@acme.io" in clean_text("<p>jane@acme.io</p>", redact_pii=False)


def test_settings_require_a_domain():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"FRESHDESK_API_KEY": "k"})


def test_settings_require_api_key_in_api_key_mode():
    with pytest.raises(ConfigurationError):
        Settings.from_env({"FRESHDESK_DOMAIN": "acme"})


def test_settings_require_full_oauth_triplet():
    with pytest.raises(ConfigurationError) as excinfo:
        Settings.from_env({"FRESHDESK_DOMAIN": "acme", "FRESHDESK_AUTH_MODE": "oauth"})
    assert "FRESHDESK_OAUTH_CLIENT_ID" in str(excinfo.value)


def test_settings_from_env_reads_overrides():
    settings = Settings.from_env(
        {
            "FRESHDESK_DOMAIN": "acme.freshdesk.com",
            "FRESHDESK_API_KEY": "k",
            "MERIDIAN_REQUESTS_PER_MINUTE": "120",
            "MERIDIAN_MAX_PAGE_SIZE": "500",
            "MERIDIAN_ALLOW_WRITES": "yes",
            "MERIDIAN_REDACT_PII": "false",
        }
    )

    assert settings.requests_per_minute == 120
    assert settings.max_page_size == 100
    assert settings.allow_writes is True
    assert settings.redact_pii is False
    assert "api_key" not in settings.redacted()
    assert settings.redacted()["credential_present"] is True
