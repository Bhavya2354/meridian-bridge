# Agent capability boundaries

What an Agent Studio agent connected to Meridian Bridge can and cannot do. Written for the
person approving the connector for a merchant account, not just for the engineer wiring it.

## Can do

**Read the queue in aggregate.** `queue_snapshot` returns counts by status and priority, escalated
and SLA-breached totals, and the five oldest unresolved tickets for a window of up to 30 days,
optionally scoped to one group. One call answers "what needs attention right now".

**Find tickets by structured criteria.** `search_tickets` filters on status, priority, tags,
ticket type, assigned agent, group, and created/updated dates. Multiple statuses or priorities are
OR-ed; different fields are AND-ed. Up to 100 tickets per call.

**Narrow results by keyword.** `search_tickets(text=...)` matches a keyword against ticket subject
and description — see the limitation on this below.

**Read recent activity.** `list_tickets` returns tickets newest-first, filterable by requester
email and `updated_since`, orderable by `created_at`, `updated_at`, `due_by` or `status`.

**Read one ticket in depth.** `get_ticket` returns all ticket fields, requester identity, Freshdesk
SLA stats, and up to 50 conversation entries, each labelled inbound/outbound and
public/private_note.

**Reason about derived state.** Every ticket carries human-readable `status`, `priority` and
`source` labels plus `age_hours` and `sla_breached`, derived by the connector so the agent does not
have to interpret Freshdesk's numeric codes or compare timestamps itself.

**See its own resource usage.** Every response includes `diagnostics` with request count, retries,
throttle events, and the remaining Freshdesk quota.

**Verify its own configuration.** `health_check` confirms the credential works and reports the
effective limits and safety flags.

**Add an internal note — only if the deployment allows it.** `add_private_note` appends an
agents-only note. It is refused with `write_not_allowed` unless `MERIDIAN_ALLOW_WRITES=true`.

## Cannot do

**Cannot contact a customer.** There is no tool that sends a public reply, email, or any outbound
message. The only write path is an internal note, and it is off by default.

**Cannot change ticket state.** No status, priority, assignee, group, tag or custom-field updates;
no create, merge, split, spam-mark, or delete.

**Cannot touch anything outside tickets.** Contacts, companies, agents, groups, products,
business hours, SLA policies, automations, surveys, time entries, and admin settings are not
exposed. `health_check` reads the authenticated agent's own record only.

**Cannot see raw customer contact details.** With `MERIDIAN_REDACT_PII=true` (default), emails are
masked (`c******@example.com`), phone numbers and card-like digit runs are replaced before any
text reaches the model — in ticket subjects and bodies, conversation entries, the requester block
and merchant-defined custom fields alike. The agent can therefore reason about a case but cannot
exfiltrate a contact list. `requester_email` on `list_tickets` is an input filter: an exact address
must already be known to use it.

**Cannot download attachments.** Attachment URLs and file contents are not returned.

**Cannot do full-text search server-side.** Freshdesk's query language indexes structured fields
only. The `text` argument is therefore applied by the connector to the slice it fetched — it is a
narrowing filter over matched tickets, not a helpdesk-wide search. For a reliable keyword hunt,
constrain the structured filters (status, date range) first.

**Cannot bulk-export.** Hard caps: 100 tickets per call, 50 conversation entries per ticket, 5
pages per request chain, bodies truncated at 4,000 characters. Responses set `truncated: true`
rather than paging forever, so the agent cannot sweep the whole helpdesk in a loop.

**Cannot outrun the merchant's rate limit.** All traffic is paced through a token bucket
(default 50 requests/minute) and slowed further as the account's hourly quota runs down. The
connector cannot be argued into going faster; there is no tool argument that bypasses pacing.

**Cannot exceed its credential.** Freshdesk enforces the API key's or OAuth token's own role and
ticket scope. An agent scoped to one group sees one group, regardless of what it asks for.

**Cannot see credentials.** Keys, secrets and tokens are never returned by any tool, including
`health_check`.

## Failure modes an agent should expect

| `error` | Meaning | Correct agent behaviour |
| --- | --- | --- |
| `rate_limited` | Quota exhausted after bounded retries | Stop; report the wait in `details.last_retry_after` |
| `invalid_request` | Bad argument, unknown status label, or search with no filter | Fix the arguments and retry once |
| `not_found` | Ticket id does not exist or is out of scope | Do not retry; say so |
| `authentication_error` | Credential rejected or expired beyond auto-refresh | Stop; the deployment needs attention |
| `permission_denied` | Credential's role forbids the resource | Stop; do not probe other ids |
| `write_not_allowed` | Writes disabled for this deployment | Report the limitation; do not retry |
| `upstream_error` | Freshdesk `5xx` or network fault after retries | Retry later, not immediately |

## Operational notes

- **Credential scope.** Provision a dedicated Freshdesk agent with the narrowest role and ticket
  scope the use case needs; the connector inherits those limits.
- **Shared quota.** The hourly quota is per account, shared with every other integration. Lower
  `MERIDIAN_REQUESTS_PER_MINUTE` when running alongside other automation.
- **Audit trail.** Enabling writes means notes appear in Freshdesk under the connector's agent
  identity — keep it a distinct agent so automated activity stays attributable.
