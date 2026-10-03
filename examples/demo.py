from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from meridian_bridge.auth import ApiKeyAuth  # noqa: E402
from meridian_bridge.client import FreshdeskClient  # noqa: E402
from meridian_bridge.config import Settings  # noqa: E402
from meridian_bridge.tickets import TicketService  # noqa: E402

SUBJECTS = [
    "Refund not received for cancelled order",
    "Wrong size delivered, need exchange",
    "Parcel stuck in transit for 6 days",
    "Coupon code rejected at checkout",
    "Duplicate charge on my card",
]
QUOTA_TOTAL = 1000


def seed_tickets(count: int = 64) -> list[dict]:
    random.seed(7)
    now = datetime.now(timezone.utc)
    tickets = []
    for index in range(1, count + 1):
        created = now - timedelta(hours=random.randint(1, 120))
        tickets.append(
            {
                "id": index,
                "subject": random.choice(SUBJECTS),
                "status": random.choice([2, 2, 3, 4, 5]),
                "priority": random.choice([1, 2, 2, 3, 4]),
                "source": random.choice([1, 2, 3]),
                "requester_id": 5000 + index,
                "email": f"customer{index}@example.com",
                "responder_id": random.choice([11, 12, None]),
                "group_id": random.choice([7, 8]),
                "type": random.choice(["Incident", "Question", "Refund"]),
                "tags": random.sample(["vip", "refund", "delivery", "payments"], k=2),
                "created_at": created.isoformat().replace("+00:00", "Z"),
                "updated_at": (created + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "due_by": (created + timedelta(hours=random.choice([4, 48]))).isoformat().replace("+00:00", "Z"),
                "fr_due_by": (created + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "is_escalated": random.random() < 0.2,
                "spam": False,
                "description": (
                    f"<p>Order #{90000 + index} is still unresolved. Mail me at customer{index}@example.com "
                    f"or call +1 415 555 0{index:03d}.</p>"
                ),
            }
        )
    return tickets


class MockFreshdesk:
    def __init__(self, tickets: list[dict]) -> None:
        self.tickets = tickets
        self.calls = 0
        self.throttle_on_call = 3

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.calls == self.throttle_on_call:
            return httpx.Response(429, headers={"Retry-After": "1", "x-ratelimit-total": str(QUOTA_TOTAL)})

        path = request.url.path
        headers = {
            "x-ratelimit-total": str(QUOTA_TOTAL),
            "x-ratelimit-remaining": str(max(QUOTA_TOTAL - self.calls, 0)),
            "x-ratelimit-used-currentrequest": "1",
        }

        if path.endswith("/agents/me"):
            return httpx.Response(200, json={"id": 11, "contact": {"name": "Queue Bot", "email": "bot@acme.io"}, "ticket_scope": 1}, headers=headers)

        if path.endswith("/search/tickets"):
            rows = self._filter(request.url.params.get("query", ""))
            page = int(request.url.params.get("page", 1))
            window = rows[(page - 1) * 30 : page * 30]
            return httpx.Response(200, json={"results": window, "total": len(rows)}, headers=headers)

        if path.endswith("/conversations"):
            ticket_id = int(path.split("/")[-2])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": ticket_id * 10,
                        "user_id": 5000 + ticket_id,
                        "incoming": True,
                        "private": False,
                        "created_at": "2026-09-01T10:05:00Z",
                        "body": "<div>Any update? My number is +1 415 555 0132.</div>",
                    },
                    {
                        "id": ticket_id * 10 + 1,
                        "user_id": 11,
                        "incoming": False,
                        "private": True,
                        "created_at": "2026-09-01T11:05:00Z",
                        "body": "<div>Checked the carrier portal, parcel is mis-routed.</div>",
                    },
                ],
                headers=headers,
            )

        if "/tickets/" in path:
            ticket_id = int(path.rsplit("/", 1)[-1])
            match = next((ticket for ticket in self.tickets if ticket["id"] == ticket_id), None)
            if not match:
                return httpx.Response(404, json={"message": "not found"}, headers=headers)
            detail = {**match, "requester": {"id": match["requester_id"], "name": "Customer", "email": match["email"]}}
            return httpx.Response(200, json=detail, headers=headers)

        page = int(request.url.params.get("page", 1))
        per_page = int(request.url.params.get("per_page", 30))
        window = self.tickets[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json=window, headers=headers)

    def _filter(self, query: str) -> list[dict]:
        wanted_status = {int(part) for part in _codes(query, "status")}
        wanted_priority = {int(part) for part in _codes(query, "priority")}
        rows = self.tickets
        if wanted_status:
            rows = [row for row in rows if row["status"] in wanted_status]
        if wanted_priority:
            rows = [row for row in rows if row["priority"] in wanted_priority]
        return rows


def _codes(query: str, field: str) -> list[str]:
    return [
        chunk.split(":")[1].strip("'\")( ")
        for chunk in query.replace("(", " ").replace(")", " ").split()
        if chunk.startswith(f"{field}:")
    ]


def show(title: str, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, indent=2, default=str)[:1800])


async def main() -> None:
    settings = Settings(domain="demo", api_key="demo-key", requests_per_minute=600)
    upstream = MockFreshdesk(seed_tickets())
    http = httpx.AsyncClient(base_url=settings.base_url, transport=httpx.MockTransport(upstream))

    async with FreshdeskClient(settings, ApiKeyAuth("demo-key"), http=http) as client:
        service = TicketService(client)
        show("health_check", {"identity": await client.verify_credentials(), "config": settings.redacted()})
        show("queue_snapshot", await service.queue_snapshot(window_hours=72))
        show("search_tickets", await service.search_tickets(status=["open"], priority=["urgent"], limit=3))
        show("list_tickets", await service.list_tickets(limit=2, include_description=True))
        show("get_ticket", await service.get_ticket(4, conversation_limit=2))
        print("\n=== connector diagnostics ===")
        print(json.dumps(client.diagnostics(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
