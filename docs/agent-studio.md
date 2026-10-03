# Wiring the connector into Agent Studio

## 1. Provision the credential

**API key (fastest path).** A free Freshdesk trial is enough to get one. In Freshdesk, create a
dedicated agent for automation, give it the narrowest role and ticket scope the use case needs,
then copy its API key from *Profile settings → Your API Key*. Set `FRESHDESK_AUTH_MODE=api_key` and `FRESHDESK_API_KEY`.

**OAuth (for multi-tenant installs).** Register the app in the Freshworks developer portal, run
the authorization-code flow once per merchant, and store the resulting refresh token. Set
`FRESHDESK_AUTH_MODE=oauth` with `FRESHDESK_OAUTH_CLIENT_ID`, `_CLIENT_SECRET`, `_REFRESH_TOKEN`
and, if your region differs, `_TOKEN_URL`. The connector exchanges the refresh token for an access
token on first use, caches it until 60s before expiry, refreshes under a single-flight lock, and
re-authorizes once automatically on a `401`.

Verify either mode before wiring anything up:

```bash
meridian-probe health
```

## 2. Register the MCP server

Local / single-tenant — stdio, see `examples/agent-studio.mcp.json`:

```json
{
  "mcpServers": {
    "meridian-bridge": {
      "command": "python",
      "args": ["-m", "meridian_bridge.mcp_server"],
      "env": {
        "FRESHDESK_DOMAIN": "acme",
        "FRESHDESK_API_KEY": "${FRESHDESK_API_KEY}",
        "MERIDIAN_REQUESTS_PER_MINUTE": "50"
      }
    }
  }
}
```

Hosted / shared — streamable HTTP:

```bash
MERIDIAN_TRANSPORT=streamable-http meridian-bridge
```

Run one process per merchant account: configuration is per-process, so tenant isolation comes
from process isolation rather than from request-level routing.

## 3. Confirm the tool surface

```bash
python scripts/export_tool_spec.py   # regenerates docs/mcp-tools.json
```

The agent should see six tools (`health_check`, `queue_snapshot`, `search_tickets`,
`list_tickets`, `get_ticket`, `add_private_note`) and the `freshdesk://ticket/{ticket_id}`
resource template. Read-only tools advertise `readOnlyHint`, so a platform that gates mutating
tools behind approval will classify them correctly without extra configuration.

## 4. Prompt the agent well

The server ships `instructions` that MCP clients surface to the model. Reinforce in the agent's
own prompt:

- Start with `queue_snapshot` for any "how is the queue doing" question — one call instead of many.
- Use `search_tickets` with structured filters before reaching for `text`.
- Call `get_ticket` only for tickets it intends to act on.
- Watch `diagnostics.quota.remaining` and stop when it approaches zero.
- Treat `{"ok": false, ...}` as data: read `error`, follow the table in
  [CAPABILITIES.md](CAPABILITIES.md), and never retry `write_not_allowed` or `not_found`.

## 5. Tune for the account

| Setting | Default | When to change |
| --- | --- | --- |
| `MERIDIAN_REQUESTS_PER_MINUTE` | 50 | Lower when other integrations share the hourly quota |
| `MERIDIAN_MAX_PAGES_PER_CALL` | 5 | Lower to tighten context cost per tool call |
| `MERIDIAN_MAX_RETRIES` | 4 | Lower for latency-sensitive interactive agents |
| `MERIDIAN_RETRY_BASE_DELAY` / `_MAX_DELAY` | 0.5 / 30 | Widen for accounts on a tight hourly quota |
| `MERIDIAN_REQUEST_TIMEOUT` | 20 | Raise only if the account is consistently slow |
| `MERIDIAN_BASE_URL` | derived from domain | Set for a gateway, proxy or local emulator |
| `MERIDIAN_REDACT_PII` | true | Keep enabled unless the agent is legally cleared for raw PII |
| `MERIDIAN_ALLOW_WRITES` | false | Enable only when internal notes are an agreed outcome |

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `configuration_error` on startup | Missing domain or credential | Check `.env`; `MERIDIAN_ENV_FILE` overrides its path |
| `authentication_error` | Wrong key, or OAuth refresh token revoked | Re-run `meridian-probe health`; reissue the credential |
| `permission_denied` on some tickets | Agent role or ticket scope is narrower than the request | Widen the Freshdesk role, or scope the agent's task |
| `rate_limited` recurring | Quota shared with other automation | Lower `MERIDIAN_REQUESTS_PER_MINUTE` |
| Empty `search_tickets` results | Query language indexes structured fields only | Add structured filters; treat `text` as a narrowing filter |
| MCP client reports a protocol error | Logs written to stdout | Logging goes to stderr by default — keep stdout clean on stdio transport |
