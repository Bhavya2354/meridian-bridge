from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from meridian_bridge.client import FreshdeskClient
from meridian_bridge.errors import ValidationError, WriteNotAllowedError
from meridian_bridge.models import (
    PRIORITY_CODES,
    PRIORITY_LABELS,
    STATUS_CODES,
    STATUS_LABELS,
    ConversationEntry,
    Ticket,
)
from meridian_bridge.sanitize import mask_email

SEARCH_PAGE_SIZE = 30
SEARCH_PAGE_CAP = 10
OPEN_STATUS_CODES = (2, 3, 6, 7)
LIST_ORDER_FIELDS = {"created_at", "updated_at", "due_by", "status"}
NOTE_MAX_CHARS = 5000


class TicketService:
    def __init__(self, client: FreshdeskClient) -> None:
        self.client = client
        self.settings = client.settings

    async def list_tickets(
        self,
        *,
        status: str | int | None = None,
        priority: str | int | None = None,
        requester_email: str | None = None,
        updated_since: str | None = None,
        order_by: str = "updated_at",
        order_type: str = "desc",
        limit: int = 25,
        include_description: bool = False,
    ) -> dict:
        limit = _bounded(limit, 1, 100, "limit")
        if order_by not in LIST_ORDER_FIELDS:
            raise ValidationError(f"order_by must be one of {sorted(LIST_ORDER_FIELDS)}")
        if order_type not in {"asc", "desc"}:
            raise ValidationError("order_type must be 'asc' or 'desc'")

        if status is not None or priority is not None:
            return await self.search_tickets(
                status=[status] if status is not None else None,
                priority=[priority] if priority is not None else None,
                updated_after=updated_since,
                limit=limit,
                include_description=include_description,
            )

        params = {
            "updated_since": _iso(updated_since, "updated_since"),
            "email": requester_email,
            "order_by": order_by,
            "order_type": order_type,
        }
        rows, truncated = await self.client.paginate("/tickets", params=params, max_items=limit)
        return self._envelope(
            rows,
            source="list",
            truncated=truncated,
            include_description=include_description,
            applied={k: v for k, v in params.items() if v is not None},
        )

    async def get_ticket(
        self,
        ticket_id: int,
        *,
        include_conversations: bool = True,
        conversation_limit: int = 10,
    ) -> dict:
        ticket_id = _bounded(ticket_id, 1, 10**12, "ticket_id")
        conversation_limit = _bounded(conversation_limit, 0, 50, "conversation_limit")
        payload = await self.client.get(f"/tickets/{ticket_id}", params={"include": "requester,stats"})
        ticket = Ticket.from_api(payload, redact_pii=self.settings.redact_pii, include_description=True)
        result = ticket.to_dict()
        result["requester"] = self._requester(payload)
        result["stats"] = payload.get("stats") or {}

        if include_conversations and conversation_limit:
            rows, truncated = await self.client.paginate(
                f"/tickets/{ticket_id}/conversations",
                per_page=min(conversation_limit, 30),
                max_items=conversation_limit,
                max_pages=2,
            )
            entries = [ConversationEntry.from_api(row, redact_pii=self.settings.redact_pii) for row in rows]
            result["conversation"] = [entry.to_dict() for entry in entries]
            result["conversation_truncated"] = truncated
        return {"ticket": result, "diagnostics": self.client.diagnostics()}

    async def search_tickets(
        self,
        *,
        status: Sequence[str | int] | None = None,
        priority: Sequence[str | int] | None = None,
        tags: Sequence[str] | None = None,
        ticket_type: str | None = None,
        agent_id: int | None = None,
        group_id: int | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        updated_after: str | None = None,
        text: str | None = None,
        limit: int = 25,
        include_description: bool = False,
    ) -> dict:
        limit = _bounded(limit, 1, 100, "limit")
        clauses: list[str] = []
        if status:
            clauses.append(_or_clause("status", [_status_code(value) for value in status]))
        if priority:
            clauses.append(_or_clause("priority", [_priority_code(value) for value in priority]))
        if tags:
            clauses.append(_or_clause("tag", [f"'{_safe(tag)}'" for tag in tags]))
        if ticket_type:
            clauses.append(f"type:'{_safe(ticket_type)}'")
        if agent_id is not None:
            clauses.append(f"agent_id:{int(agent_id)}")
        if group_id is not None:
            clauses.append(f"group_id:{int(group_id)}")
        if created_after:
            clauses.append(f"created_at:>'{_date(created_after, 'created_after')}'")
        if created_before:
            clauses.append(f"created_at:<'{_date(created_before, 'created_before')}'")
        if updated_after:
            clauses.append(f"updated_at:>'{_date(updated_after, 'updated_after')}'")

        if not clauses:
            raise ValidationError("search requires at least one structured filter")

        fql = " AND ".join(clauses)
        fetch_limit = min(limit * 3, 300) if text else limit
        rows, total, truncated = await self._search(fql, fetch_limit)

        if text:
            needle = text.strip().lower()
            rows = [row for row in rows if _matches_text(row, needle)]
            truncated = truncated or len(rows) > limit
            rows = rows[:limit]

        return self._envelope(
            rows,
            source="search",
            truncated=truncated,
            include_description=include_description,
            applied={"query": fql, "keyword": text, "match_estimate": total},
        )

    async def queue_snapshot(self, *, window_hours: int = 24, group_id: int | None = None) -> dict:
        window_hours = _bounded(window_hours, 1, 720, "window_hours")
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        clauses = [_or_clause("status", list(OPEN_STATUS_CODES)), f"updated_at:>'{since.date().isoformat()}'"]
        if group_id is not None:
            clauses.append(f"group_id:{int(group_id)}")
        rows, total, truncated = await self._search(" AND ".join(clauses), 300)

        tickets = [Ticket.from_api(row, redact_pii=self.settings.redact_pii) for row in rows]
        by_status = Counter(ticket.status for ticket in tickets)
        by_priority = Counter(ticket.priority for ticket in tickets)
        breached = [ticket for ticket in tickets if ticket.sla_breached]
        oldest = sorted(
            (ticket for ticket in tickets if ticket.age_hours is not None),
            key=lambda ticket: ticket.age_hours or 0,
            reverse=True,
        )[:5]

        return {
            "window_hours": window_hours,
            "group_id": group_id,
            "sampled_tickets": len(tickets),
            "match_estimate": total,
            "sample_truncated": truncated,
            "by_status": dict(by_status),
            "by_priority": dict(by_priority),
            "escalated": sum(1 for ticket in tickets if ticket.is_escalated),
            "sla_breached": len(breached),
            "sla_breached_ids": [ticket.id for ticket in breached][:25],
            "oldest_open": [
                {"id": ticket.id, "subject": ticket.subject, "age_hours": ticket.age_hours, "priority": ticket.priority}
                for ticket in oldest
            ],
            "diagnostics": self.client.diagnostics(),
        }

    async def add_private_note(self, ticket_id: int, body: str) -> dict:
        if not self.settings.allow_writes:
            raise WriteNotAllowedError(
                "writes are disabled; set MERIDIAN_ALLOW_WRITES=true to allow private notes"
            )
        ticket_id = _bounded(ticket_id, 1, 10**12, "ticket_id")
        text = (body or "").strip()
        if not text:
            raise ValidationError("note body must not be empty")
        if len(text) > NOTE_MAX_CHARS:
            raise ValidationError(f"note body exceeds {NOTE_MAX_CHARS} characters")
        payload = await self.client.post(
            f"/tickets/{ticket_id}/notes", json={"body": text, "private": True}
        )
        return {
            "note_id": payload.get("id"),
            "ticket_id": ticket_id,
            "private": True,
            "created_at": payload.get("created_at"),
        }

    async def _search(self, fql: str, limit: int) -> tuple[list[dict], int | None, bool]:
        results: list[dict] = []
        total: int | None = None
        pages = min(SEARCH_PAGE_CAP, self.settings.max_pages_per_call + 1)
        for page in range(1, pages + 1):
            response = await self.client.request(
                "GET", "/search/tickets", params={"query": f'"{fql}"', "page": page}
            )
            payload = response.data or {}
            batch = payload.get("results", []) if isinstance(payload, dict) else []
            total = payload.get("total", total) if isinstance(payload, dict) else total
            results.extend(batch)
            if len(results) >= limit or len(batch) < SEARCH_PAGE_SIZE:
                break
        truncated = len(results) > limit or bool(total and total > len(results))
        return results[:limit], total, truncated

    def _envelope(
        self,
        rows: Iterable[dict],
        *,
        source: str,
        truncated: bool,
        include_description: bool,
        applied: dict,
    ) -> dict:
        tickets = [
            Ticket.from_api(
                row, redact_pii=self.settings.redact_pii, include_description=include_description
            ).to_dict()
            for row in rows
        ]
        return {
            "tickets": tickets,
            "count": len(tickets),
            "truncated": truncated,
            "source": source,
            "filters_applied": {key: value for key, value in applied.items() if value is not None},
            "diagnostics": self.client.diagnostics(),
        }

    def _requester(self, payload: dict) -> dict:
        requester = payload.get("requester") or {}
        email = requester.get("email")
        return {
            "id": requester.get("id") or payload.get("requester_id"),
            "name": requester.get("name"),
            "email": mask_email(email) if (email and self.settings.redact_pii) else email,
        }


def _matches_text(row: dict, needle: str) -> bool:
    haystack = " ".join(
        str(row.get(field) or "") for field in ("subject", "description_text", "description")
    ).lower()
    return needle in haystack


def _or_clause(field: str, values: Sequence) -> str:
    unique = list(dict.fromkeys(values))
    if len(unique) == 1:
        return f"{field}:{unique[0]}"
    return "(" + " OR ".join(f"{field}:{value}" for value in unique) + ")"


def _status_code(value: str | int) -> int:
    if isinstance(value, int) or str(value).isdigit():
        code = int(value)
        if code not in STATUS_LABELS:
            raise ValidationError(f"unknown status code {code}; expected one of {sorted(STATUS_LABELS)}")
        return code
    label = str(value).strip().lower()
    if label not in STATUS_CODES:
        raise ValidationError(f"unknown status '{value}'; expected one of {sorted(STATUS_CODES)}")
    return STATUS_CODES[label]


def _priority_code(value: str | int) -> int:
    if isinstance(value, int) or str(value).isdigit():
        code = int(value)
        if code not in PRIORITY_LABELS:
            raise ValidationError(f"unknown priority code {code}; expected 1-4")
        return code
    label = str(value).strip().lower()
    if label not in PRIORITY_CODES:
        raise ValidationError(f"unknown priority '{value}'; expected one of {sorted(PRIORITY_CODES)}")
    return PRIORITY_CODES[label]


def _safe(value: str) -> str:
    cleaned = str(value).replace("'", "").replace('"', "").strip()
    if not cleaned:
        raise ValidationError("filter values must not be empty")
    return cleaned


def _bounded(value, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    if not low <= number <= high:
        raise ValidationError(f"{name} must be between {low} and {high}")
    return number


def _iso(value: str | None, name: str) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value: str, name: str) -> str:
    iso = _iso(value, name)
    return (iso or "")[:10]
