from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = "live-test-key"
QUOTA_TOTAL = 1000
PAGE_CAP = 100
SEARCH_PAGE = 30
SUBJECTS = [
    "Refund not received for cancelled order",
    "Wrong size delivered, need exchange",
    "Parcel stuck in transit",
    "Coupon code rejected at checkout",
    "Duplicate charge on my card",
]


def seed_tickets(count: int = 75) -> list[dict]:
    now = datetime.now(timezone.utc)
    tickets = []
    for index in range(1, count + 1):
        created = now - timedelta(hours=index)
        overdue = index % 3 == 0
        tickets.append(
            {
                "id": index,
                "subject": SUBJECTS[index % len(SUBJECTS)],
                "status": [2, 2, 3, 4, 5][index % 5],
                "priority": [1, 2, 3, 4][index % 4],
                "source": [1, 2, 3][index % 3],
                "requester_id": 5000 + index,
                "email": f"customer{index}@example.com",
                "responder_id": 11,
                "group_id": 7 if index % 2 else 8,
                "type": ["Incident", "Question", "Refund"][index % 3],
                "tags": ["vip", "refund"] if index % 4 == 0 else ["delivery"],
                "created_at": _iso(created),
                "updated_at": _iso(created + timedelta(hours=1)),
                "due_by": _iso(created - timedelta(hours=2) if overdue else now + timedelta(days=2)),
                "fr_due_by": _iso(created + timedelta(hours=2)),
                "is_escalated": index % 7 == 0,
                "spam": False,
                "description": (
                    f"<p>Order #{90000 + index} is unresolved. Mail customer{index}@example.com "
                    f"or call +1 415 555 0{index:03d}.</p>"
                ),
            }
        )
    return tickets


class FreshdeskEmulator:
    def __init__(self, *, tickets: list[dict] | None = None, throttle_on_call: int | None = None) -> None:
        self.tickets = tickets if tickets is not None else seed_tickets()
        self.throttle_on_call = throttle_on_call
        self.requests: list[tuple[str, str]] = []
        self.notes: list[dict] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FreshdeskEmulator":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/api/v2"

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def record(self, method: str, path: str) -> int:
        with self._lock:
            self.requests.append((method, path))
            return len(self.requests)

    def paths(self, needle: str) -> list[str]:
        return [path for _, path in self.requests if needle in path]


def _make_handler(state: FreshdeskEmulator):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:
            return

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            call = state.record(method, parsed.path)

            if not self._authorized():
                return self._send(401, {"code": "invalid_credentials", "message": "Please log in"})

            if state.throttle_on_call == call:
                return self._send(429, {"message": "rate limit exceeded"}, extra={"Retry-After": "1"})

            if parsed.path.endswith("/agents/me"):
                return self._send(
                    200,
                    {"id": 11, "ticket_scope": 1, "contact": {"name": "Queue Bot", "email": "bot@acme.io"}},
                )

            if parsed.path.endswith("/search/tickets"):
                return self._search(params)

            if parsed.path.endswith("/conversations"):
                return self._conversations(parsed.path, params)

            if method == "POST" and parsed.path.endswith("/notes"):
                return self._note(parsed.path)

            match = re.fullmatch(r"/api/v2/tickets/(\d+)", parsed.path)
            if match:
                return self._ticket(int(match.group(1)))

            if parsed.path.endswith("/tickets"):
                return self._list(params)

            self._send(404, {"message": "unknown endpoint"})

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                decoded = base64.b64decode(header[6:]).decode(errors="ignore")
                return decoded.split(":")[0] == API_KEY
            return header == f"Bearer {API_KEY}"

        def _list(self, params: dict) -> None:
            page = int(params.get("page", 1))
            per_page = min(int(params.get("per_page", 30)), PAGE_CAP)
            rows = sorted(state.tickets, key=lambda row: row["updated_at"], reverse=True)
            if params.get("email"):
                rows = [row for row in rows if row["email"] == params["email"]]
            if params.get("updated_since"):
                rows = [row for row in rows if row["updated_at"] >= params["updated_since"]]
            window = rows[(page - 1) * per_page : page * per_page]
            extra = {}
            if page * per_page < len(rows):
                extra["Link"] = f'<{state.base_url}/tickets?page={page + 1}>; rel="next"'
            self._send(200, window, extra=extra)

        def _search(self, params: dict) -> None:
            query = params.get("query", "").strip('"')
            rows = state.tickets
            statuses = _codes(query, "status")
            priorities = _codes(query, "priority")
            groups = _codes(query, "group_id")
            if statuses:
                rows = [row for row in rows if row["status"] in statuses]
            if priorities:
                rows = [row for row in rows if row["priority"] in priorities]
            if groups:
                rows = [row for row in rows if row["group_id"] in groups]
            page = int(params.get("page", 1))
            window = rows[(page - 1) * SEARCH_PAGE : page * SEARCH_PAGE]
            self._send(200, {"results": window, "total": len(rows)})

        def _ticket(self, ticket_id: int) -> None:
            row = next((item for item in state.tickets if item["id"] == ticket_id), None)
            if not row:
                return self._send(404, {"message": "Ticket not found"})
            detail = {
                **row,
                "requester": {"id": row["requester_id"], "name": f"Customer {ticket_id}", "email": row["email"]},
                "stats": {"first_responded_at": row["fr_due_by"], "resolved_at": None},
            }
            self._send(200, detail)

        def _conversations(self, path: str, params: dict) -> None:
            ticket_id = int(path.split("/")[-2])
            per_page = min(int(params.get("per_page", 30)), PAGE_CAP)
            entries = [
                {
                    "id": ticket_id * 100 + index,
                    "user_id": 5000 + ticket_id if index % 2 == 0 else 11,
                    "incoming": index % 2 == 0,
                    "private": index % 2 == 1,
                    "created_at": _iso(datetime.now(timezone.utc) - timedelta(hours=6 - index)),
                    "body": f"<div>Message {index}. Reach me on +1 415 555 0132.</div>",
                }
                for index in range(4)
            ]
            self._send(200, entries[:per_page])

        def _note(self, path: str) -> None:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            note = {"id": 9000 + len(state.notes), "ticket_id": int(path.split("/")[-2]), **payload}
            state.notes.append(note)
            self._send(201, {**note, "created_at": _iso(datetime.now(timezone.utc))})

        def _send(self, status: int, payload, *, extra: dict | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-RateLimit-Total", str(QUOTA_TOTAL))
            self.send_header("X-RateLimit-Remaining", str(max(QUOTA_TOTAL - state.call_count, 0)))
            self.send_header("X-RateLimit-Used-CurrentRequest", "1")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _codes(query: str, field: str) -> set[int]:
    return {int(value) for value in re.findall(rf"{field}:(\d+)", query)}


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")
