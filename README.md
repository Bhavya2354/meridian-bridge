# Meridian Bridge — a private Freshdesk connector for Agent Studio

Meridian Bridge is an MCP server that gives an Agent Studio agent safe, read-first access to a
merchant's Freshdesk helpdesk: list and search tickets, read a ticket with its conversation
thread, and get an aggregated view of the open queue — without handing the model raw customer
PII or letting it exhaust the merchant's shared API quota.

```
Agent Studio agent
      │  MCP (stdio / SSE / streamable-http)
      ▼
┌──────────────────────────────────────────────────────────┐
│ mcp_server.py      6 tools + 1 resource template         │
│ tickets.py         list / get / search / snapshot        │
│ models.py          Freshdesk payload → agent-shaped JSON │
│ sanitize.py        HTML → text, PII masking              │
│ client.py          retries, pagination, error mapping    │
│ ratelimit.py       token bucket + quota-header feedback  │
│ auth.py            API key (Basic) or OAuth2 refresh     │
└──────────────────────────────────────────────────────────┘
      │  HTTPS
      ▼
Freshdesk REST API v2
```

## Quickstart

```bash
pip install -e ".[dev]"
cp .env.example .env            # fill in FRESHDESK_DOMAIN + FRESHDESK_API_KEY

python examples/demo.py         # full walkthrough, no credentials needed
meridian-probe health           # verify the real credential
meridian-probe snapshot --window-hours 48
meridian-bridge                 # run the MCP server on stdio
```

No Freshdesk account is needed to review this. `examples/demo.py` runs every primitive against
an in-process Freshdesk stand-in, and `tests/test_end_to_end.py` goes further: it boots a
Freshdesk emulator on a real socket and drives the connector over real HTTP — then over the real
MCP protocol, with an MCP client spawning the server as a subprocess exactly as an agent runtime
would.

```bash
python -m pytest -q tests/test_end_to_end.py
```

Pointing the connector at a local server (or a merchant's gateway/proxy) is a supported
configuration, not a test hook: `MERIDIAN_BASE_URL` overrides the derived API root, and accepts
plain `http://` only for loopback hosts.

## Tools exposed to the agent

| Tool | Purpose | Key arguments |
| --- | --- | --- |
| `health_check` | Validate the credential, report effective config and quota | — |
| `queue_snapshot` | Aggregate the unresolved queue before reading tickets | `window_hours`, `group_id` |
| `search_tickets` | Filter by status / priority / tag / type / agent / group / date, optional keyword | `status[]`, `priority[]`, `tags[]`, `text`, `limit` |
| `list_tickets` | Recent activity feed, newest first | `requester_email`, `updated_since`, `order_by`, `limit` |
| `get_ticket` | One ticket in full with its conversation thread | `ticket_id`, `conversation_limit` |
| `add_private_note` | Internal note, agents-only — **disabled by default** | `ticket_id`, `body` |

Resource template: `freshdesk://ticket/{ticket_id}`.
The machine-readable spec lives in [docs/mcp-tools.json](docs/mcp-tools.json), regenerated with
`python scripts/export_tool_spec.py`. Behavioural boundaries are documented in
[docs/CAPABILITIES.md](docs/CAPABILITIES.md); wiring instructions in
[docs/agent-studio.md](docs/agent-studio.md).

## Design decisions

**Authentication — two modes, one interface.** `AuthProvider` has two implementations:
`ApiKeyAuth` (HTTP Basic with the API key as username and `X` as password, the scheme Freshdesk
documents) and `OAuth2Auth` (refresh-token grant, cached access token, single-flight refresh
under an `asyncio.Lock`, 60s expiry skew). A `401` triggers exactly one credential invalidation
and retry, so a token that expires mid-session recovers silently instead of surfacing as an
agent-visible failure. Mode is chosen by `FRESHDESK_AUTH_MODE`; nothing else in the stack changes.

**Rate limits — pace first, retry second.** Freshdesk enforces a per-account hourly quota shared
with every other integration the merchant runs, so a connector that only reacts to `429` is
already a bad neighbour. Every request passes through a token bucket (default 50/min), and each
response feeds `X-Ratelimit-Total` / `-Remaining` back into the throttle. Below a 10% quota
floor, requests are paced progressively; on `429` the connector honours `Retry-After`, otherwise
it uses exponential backoff with jitter, capped and bounded by `MERIDIAN_MAX_RETRIES`. `5xx` and
network faults retry only for idempotent methods — a `POST` is never replayed. Live counters ride
along in every tool response under `diagnostics`, so the agent can see the budget it is spending.

**Bounded responses.** Pagination follows Freshdesk's `Link` header but stops at a page and item
budget, and every response carries `truncated` and `count`. Ticket bodies are flattened from HTML
to text and capped, so one verbose ticket cannot flood the context window.

**Payloads shaped for a model, not for a database.** Freshdesk returns `status: 2`, `priority: 4`,
`source: 1`. The connector emits `"open"`, `"urgent"`, `"email"` alongside the raw codes, and
derives the fields an agent actually reasons about — `age_hours`, `sla_breached`, thread
`direction` and `visibility`. `queue_snapshot` exists for the same reason: triage questions
("what is on fire?") should cost one call, not fifty.

**Filters routed to the endpoint that supports them.** Freshdesk's list endpoint cannot filter by
status or priority — only its query-language search endpoint can. Rather than exposing that seam
to the agent, `list_tickets` transparently re-routes such calls to search and reports which
endpoint served the result in `source`.

**Privacy at the boundary.** Emails, phone numbers and card-like digit runs are masked in every
subject, description and conversation body before leaving the connector (`MERIDIAN_REDACT_PII`).
Credentials never appear in a tool result; `health_check` reports only whether one is present.

**Read-first by default.** Five of six tools are read-only and advertise `readOnlyHint`. The one
write tool posts an internal, agents-only note and refuses with `write_not_allowed` unless the
deployment sets `MERIDIAN_ALLOW_WRITES=true`. There is no tool that can reply to a customer,
change a ticket's state, or delete anything.

**Failures arrive as data.** Connector errors become `{"ok": false, "error": "...", "message":
"...", "details": {...}}` with stable machine-readable codes (`rate_limited`, `not_found`,
`invalid_request`, `authentication_error`, `write_not_allowed`), because an agent can reason about
a typed payload and cannot reason about a stack trace.

## Layout

```
src/meridian_bridge/   config, errors, auth, ratelimit, client, models, sanitize, tickets,
                       mcp_server (tool surface), cli (field-debug probe)
tests/                 150 tests — unit coverage over a mocked transport plus end-to-end runs
                       against tests/emulator.py over real HTTP and the real MCP protocol
examples/demo.py       offline end-to-end walkthrough
examples/agent-studio.mcp.json
scripts/               tool-spec exporter
docs/                  CAPABILITIES.md, agent-studio.md, mcp-tools.json
```

## Tests

```bash
python -m pytest -q
```

No Freshdesk account required, and nothing leaves the machine. Unit tests drive the real client
through an `httpx.MockTransport` with an injected clock and sleeper, so backoff and pacing are
asserted deterministically rather than waited on.

`tests/test_end_to_end.py` covers the paths a mock cannot prove: real Basic-auth headers accepted
and a wrong key rejected by a server, real `Link`-header pagination, a real `429` with
`Retry-After` retried to success, a real `404` mapped to `not_found`, a note actually written and
read back, and an MCP client calling every tool over stdio — asserting the agent sees masked PII,
no credential in any payload, and `write_not_allowed` until writes are enabled.

`tests/test_env.py` additionally enforces the configuration contract: every variable the code
reads is documented in `.env.example`, `.env.example` documents nothing the code ignores, every
`Settings` field is reachable from the environment, and each variable's default, parsing, bounds
and failure mode is asserted individually. A variable added to the code but left out of the docs
fails the suite.
