from __future__ import annotations

import json

import httpx
import pytest

from meridian_bridge.config import Settings
from meridian_bridge.errors import ValidationError, WriteNotAllowedError
from tests.conftest import make_service, ticket_payload


def search_response(rows: list[dict], total: int | None = None) -> httpx.Response:
    return httpx.Response(200, json={"results": rows, "total": total if total is not None else len(rows)})


async def test_list_tickets_maps_fields_and_masks_pii(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/tickets")
        assert request.url.params["order_by"] == "updated_at"
        return httpx.Response(200, json=[ticket_payload(1)])

    result = await make_service(settings, handler).list_tickets(limit=5, include_description=True)
    ticket = result["tickets"][0]

    assert result["source"] == "list"
    assert ticket["status"] == "open"
    assert ticket["priority"] == "high"
    assert ticket["source"] == "email"
    assert ticket["requester_email"] == "s******@example.com"
    assert "shopper@example.com" not in ticket["description"]
    assert ticket["description"].startswith("My parcel is late")
    assert result["diagnostics"]["http"]["requests"] == 1


async def test_list_tickets_routes_status_filter_through_search(settings):
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return search_response([ticket_payload(2, status=3)])

    result = await make_service(settings, handler).list_tickets(status="pending", limit=5)

    assert "/search/tickets" in captured[0]
    assert result["source"] == "search"
    assert result["filters_applied"]["query"] == "status:3"
    assert result["tickets"][0]["status"] == "pending"


async def test_search_builds_query_language_clauses(settings):
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params["query"]
        return search_response([ticket_payload(3)])

    await make_service(settings, handler).search_tickets(
        status=["open", "pending"],
        priority=[4],
        tags=["vip", "refund"],
        group_id=7,
        created_after="2026-08-01",
        limit=10,
    )

    query = captured["query"]
    assert query.startswith('"') and query.endswith('"')
    assert "(status:2 OR status:3)" in query
    assert "priority:4" in query
    assert "(tag:'vip' OR tag:'refund')" in query
    assert "group_id:7" in query
    assert "created_at:>'2026-08-01'" in query


async def test_search_rejects_empty_filters(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValidationError):
        await make_service(settings, handler).search_tickets(limit=5)


async def test_search_rejects_unknown_status(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValidationError):
        await make_service(settings, handler).search_tickets(status=["sleeping"])


async def test_search_keyword_filters_client_side(settings):
    rows = [
        ticket_payload(1, subject="Refund for duplicate charge"),
        ticket_payload(2, subject="Where is my parcel", description="<p>no refund asked</p>"),
        ticket_payload(3, subject="Change of address"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return search_response(rows)

    result = await make_service(settings, handler).search_tickets(status=["open"], text="refund", limit=10)

    assert [ticket["id"] for ticket in result["tickets"]] == [1, 2]
    assert result["filters_applied"]["keyword"] == "refund"


async def test_search_stops_paging_once_limit_is_met(settings):
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        pages.append(page)
        return search_response([ticket_payload(page * 30 + i) for i in range(30)], total=300)

    result = await make_service(settings, handler).search_tickets(status=["open"], limit=45)

    assert pages == [1, 2]
    assert result["count"] == 45
    assert result["truncated"] is True


async def test_get_ticket_includes_conversation_and_requester(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/conversations"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "user_id": 900,
                        "incoming": True,
                        "private": False,
                        "created_at": "2026-09-01T10:05:00Z",
                        "body": "<div>Still waiting. Call me on +1 415 555 0132</div>",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                **ticket_payload(42),
                "requester": {"id": 900, "name": "Shopper", "email": "shopper@example.com"},
                "stats": {"first_responded_at": "2026-09-01T11:00:00Z"},
            },
        )

    result = await make_service(settings, handler).get_ticket(42, conversation_limit=5)
    ticket = result["ticket"]

    assert ticket["id"] == 42
    assert ticket["requester"]["name"] == "Shopper"
    assert ticket["stats"]["first_responded_at"]
    entry = ticket["conversation"][0]
    assert entry["direction"] == "inbound"
    assert entry["visibility"] == "public"
    assert "[redacted-phone]" in entry["body"]


async def test_requester_block_is_masked_like_every_other_field(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                **ticket_payload(3),
                "requester": {"id": 900, "name": "Shopper", "email": "shopper@example.com"},
            },
        )

    result = await make_service(settings, handler).get_ticket(3, include_conversations=False)

    assert result["ticket"]["requester"]["email"] == "s******@example.com"
    assert "shopper@example.com" not in json.dumps(result)


async def test_requester_email_is_untouched_when_redaction_is_disabled():
    raw = Settings(domain="acme", api_key="k", redact_pii=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={**ticket_payload(3), "requester": {"id": 900, "name": "Shopper", "email": "shopper@example.com"}},
        )

    result = await make_service(raw, handler).get_ticket(3, include_conversations=False)

    assert result["ticket"]["requester"]["email"] == "shopper@example.com"


async def test_custom_fields_are_scrubbed(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                ticket_payload(
                    4,
                    custom_fields={
                        "cf_contact": "reach me at vip@example.com",
                        "cf_order_value": 1299,
                        "cf_flagged": True,
                    },
                )
            ],
        )

    result = await make_service(settings, handler).list_tickets(limit=1)
    fields = result["tickets"][0]["custom_fields"]

    assert "vip@example.com" not in fields["cf_contact"]
    assert fields["cf_order_value"] == 1299
    assert fields["cf_flagged"] is True


async def test_get_ticket_can_skip_conversation(settings):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=ticket_payload(7))

    result = await make_service(settings, handler).get_ticket(7, include_conversations=False)

    assert "conversation" not in result["ticket"]
    assert len(calls) == 1


async def test_sla_breach_is_derived_for_open_tickets(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                ticket_payload(1, due_by="2020-01-01T00:00:00Z", status=2),
                ticket_payload(2, due_by="2020-01-01T00:00:00Z", status=5),
            ],
        )

    result = await make_service(settings, handler).list_tickets(limit=10)

    assert result["tickets"][0]["sla_breached"] is True
    assert result["tickets"][1]["sla_breached"] is False


async def test_queue_snapshot_aggregates_open_queue(settings):
    rows = [
        ticket_payload(1, status=2, priority=4, due_by="2020-01-01T00:00:00Z"),
        ticket_payload(2, status=3, priority=2, is_escalated=True, due_by="2099-01-01T00:00:00Z"),
        ticket_payload(3, status=2, priority=2, created_at="2026-01-01T00:00:00Z", due_by="2099-01-01T00:00:00Z"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "status:2 OR status:3" in request.url.params["query"]
        return search_response(rows)

    snapshot = await make_service(settings, handler).queue_snapshot(window_hours=48)

    assert snapshot["sampled_tickets"] == 3
    assert snapshot["by_status"] == {"open": 2, "pending": 1}
    assert snapshot["by_priority"] == {"urgent": 1, "medium": 2}
    assert snapshot["escalated"] == 1
    assert snapshot["sla_breached_ids"] == [1]
    assert snapshot["oldest_open"][0]["id"] == 3


async def test_writes_are_denied_by_default(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(WriteNotAllowedError):
        await make_service(settings, handler).add_private_note(1, "internal context")


async def test_private_note_is_posted_when_writes_are_enabled():
    enabled = Settings(domain="acme", api_key="k", allow_writes=True)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(201, json={"id": 555, "created_at": "2026-09-02T12:00:00Z"})

    result = await make_service(enabled, handler).add_private_note(9, "  customer called back  ")

    assert captured == {"body": "customer called back", "private": True}
    assert result["note_id"] == 555


async def test_invalid_arguments_are_rejected_before_any_call(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    service = make_service(settings, handler)
    with pytest.raises(ValidationError):
        await service.list_tickets(limit=5000)
    with pytest.raises(ValidationError):
        await service.list_tickets(order_by="subject")
    with pytest.raises(ValidationError):
        await service.list_tickets(updated_since="last tuesday")
