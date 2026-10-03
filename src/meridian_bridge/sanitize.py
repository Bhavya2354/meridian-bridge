from __future__ import annotations

import html
import re

BLOCK_BREAK = re.compile(r"(?i)</(p|div|tr|li|h[1-6]|blockquote)>|<br\s*/?>")
TAG = re.compile(r"<[^>]+>")
SCRIPT_STYLE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
WHITESPACE = re.compile("[ \\t\\x0b\\f\\r\\xa0\\u200b]+")
BLANK_LINES = re.compile(r"\n{3,}")

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{7,}\d)(?!\w)")
CARD = re.compile(r"\b(?:\d[ -]*?){13,16}\b")


def html_to_text(value: str | None) -> str:
    if not value:
        return ""
    text = SCRIPT_STYLE.sub(" ", value)
    text = BLOCK_BREAK.sub("\n", text)
    text = TAG.sub("", text)
    text = html.unescape(text)
    text = WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return BLANK_LINES.sub("\n\n", text).strip()


def redact(value: str | None) -> str:
    if not value:
        return ""
    text = CARD.sub("[redacted-card]", value)
    text = EMAIL.sub(_mask_email, text)
    return PHONE.sub("[redacted-phone]", text)


def mask_email(value: str | None) -> str:
    return _mask_email_value(value) if value else ""


def clean_text(value: str | None, *, limit: int = 4000, redact_pii: bool = True) -> str:
    text = html_to_text(value)
    if redact_pii:
        text = redact(text)
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n[truncated at {limit} characters]"
    return text


def _mask_email(match: re.Match[str]) -> str:
    return _mask_email_value(match.group(0))


def _mask_email_value(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return "[redacted]"
    head = local[0] if local else "*"
    return f"{head}{'*' * max(len(local) - 1, 2)}@{domain}"
