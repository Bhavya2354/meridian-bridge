from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from meridian_bridge.sanitize import clean_text, mask_email, redact

STATUS_LABELS = {2: "open", 3: "pending", 4: "resolved", 5: "closed", 6: "waiting_on_customer", 7: "waiting_on_third_party"}
PRIORITY_LABELS = {1: "low", 2: "medium", 3: "high", 4: "urgent"}
SOURCE_LABELS = {1: "email", 2: "portal", 3: "phone", 7: "chat", 9: "feedback_widget", 10: "outbound_email"}

STATUS_CODES = {label: code for code, label in STATUS_LABELS.items()}
PRIORITY_CODES = {label: code for code, label in PRIORITY_LABELS.items()}


@dataclass(frozen=True)
class Ticket:
    id: int
    subject: str
    status: str
    status_code: int
    priority: str
    priority_code: int
    source: str
    requester_id: int | None
    requester_email: str | None
    responder_id: int | None
    group_id: int | None
    type: str | None
    tags: list[str]
    created_at: str | None
    updated_at: str | None
    due_by: str | None
    first_response_due_by: str | None
    is_escalated: bool
    spam: bool
    age_hours: float | None
    sla_breached: bool
    description: str | None = None
    custom_fields: dict | None = None

    @classmethod
    def from_api(cls, payload: dict, *, redact_pii: bool = True, include_description: bool = False) -> "Ticket":
        status_code = int(payload.get("status") or 0)
        priority_code = int(payload.get("priority") or 0)
        created_at = payload.get("created_at")
        due_by = payload.get("due_by")
        email = payload.get("email")
        return cls(
            id=int(payload["id"]),
            subject=clean_text(payload.get("subject"), limit=300, redact_pii=redact_pii),
            status=STATUS_LABELS.get(status_code, f"status_{status_code}"),
            status_code=status_code,
            priority=PRIORITY_LABELS.get(priority_code, f"priority_{priority_code}"),
            priority_code=priority_code,
            source=SOURCE_LABELS.get(int(payload.get("source") or 0), "other"),
            requester_id=payload.get("requester_id"),
            requester_email=mask_email(email) if (email and redact_pii) else email,
            responder_id=payload.get("responder_id"),
            group_id=payload.get("group_id"),
            type=payload.get("type"),
            tags=list(payload.get("tags") or []),
            created_at=created_at,
            updated_at=payload.get("updated_at"),
            due_by=due_by,
            first_response_due_by=payload.get("fr_due_by"),
            is_escalated=bool(payload.get("is_escalated")),
            spam=bool(payload.get("spam")),
            age_hours=hours_since(created_at),
            sla_breached=is_past(due_by) and status_code in (2, 3, 6, 7),
            description=(
                clean_text(payload.get("description") or payload.get("description_text"), redact_pii=redact_pii)
                if include_description
                else None
            ),
            custom_fields=scrub_fields(payload.get("custom_fields"), redact_pii=redact_pii) or None,
        )

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "subject": self.subject,
            "status": self.status,
            "priority": self.priority,
            "source": self.source,
            "type": self.type,
            "tags": self.tags,
            "requester_id": self.requester_id,
            "requester_email": self.requester_email,
            "assigned_agent_id": self.responder_id,
            "group_id": self.group_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "due_by": self.due_by,
            "first_response_due_by": self.first_response_due_by,
            "age_hours": self.age_hours,
            "is_escalated": self.is_escalated,
            "sla_breached": self.sla_breached,
            "spam": self.spam,
        }
        if self.description is not None:
            data["description"] = self.description
        if self.custom_fields:
            data["custom_fields"] = self.custom_fields
        return data


@dataclass(frozen=True)
class ConversationEntry:
    id: int
    author_id: int | None
    incoming: bool
    private: bool
    created_at: str | None
    body: str

    @classmethod
    def from_api(cls, payload: dict, *, redact_pii: bool = True, limit: int = 2000) -> "ConversationEntry":
        return cls(
            id=int(payload["id"]),
            author_id=payload.get("user_id"),
            incoming=bool(payload.get("incoming")),
            private=bool(payload.get("private")),
            created_at=payload.get("created_at"),
            body=clean_text(payload.get("body_text") or payload.get("body"), limit=limit, redact_pii=redact_pii),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "author_id": self.author_id,
            "direction": "inbound" if self.incoming else "outbound",
            "visibility": "private_note" if self.private else "public",
            "created_at": self.created_at,
            "body": self.body,
        }


def scrub_fields(fields: dict | None, *, redact_pii: bool) -> dict:
    if not fields:
        return {}
    if not redact_pii:
        return dict(fields)
    return {
        key: redact(value) if isinstance(value, str) else value
        for key, value in fields.items()
    }


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def hours_since(value: str | None) -> float | None:
    parsed = parse_timestamp(value)
    if not parsed:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return round(delta.total_seconds() / 3600, 2)


def is_past(value: str | None) -> bool:
    parsed = parse_timestamp(value)
    return bool(parsed and parsed < datetime.now(timezone.utc))
