from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from meridian_bridge.client import FreshdeskClient
from meridian_bridge.config import Settings
from meridian_bridge.errors import AuthenticationError, NotFoundError
from meridian_bridge.tickets import TicketService
from tests.emulator import API_KEY, FreshdeskEmulator

ROOT = Path(__file__).resolve().parents[1]


def live_settings(emulator: FreshdeskEmulator, **overrides) -> Settings:
    return Settings(
        domain="acme",
        base_url_override=emulator.base_url,
        api_key=API_KEY,
        requests_per_minute=600,
        **overrides,
    )


def server_env(emulator: FreshdeskEmulator, **overrides) -> dict[str, str]:
    return {
        **os.environ,
        "FRESHDESK_DOMAIN": "acme",
        "FRESHDESK_AUTH_MODE": "api_key",
        "FRESHDESK_API_KEY": API_KEY,
        "MERIDIAN_BASE_URL": emulator.base_url,
        "MERIDIAN_REQUESTS_PER_MINUTE": "600",
        "MERIDIAN_ENV_FILE": str(ROOT / "nonexistent.env"),
        "PYTHONPATH": str(ROOT / "src"),
        **overrides,
    }


@pytest.fixture
def emulator():
    with FreshdeskEmulator() as running:
        yield running


async def test_credentials_are_verified_over_real_http(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        identity = await client.verify_credentials()

    assert identity == {
        "auth_mode": "api_key",
        "account": "acme",
        "agent_id": 11,
        "agent_name": "Queue Bot",
        "agent_email": "bot@acme.io",
        "scopes": 1,
    }
    assert emulator.paths("/agents/me")


async def test_wrong_key_is_rejected_by_the_server(emulator):
    settings = live_settings(emulator)
    async with FreshdeskClient(Settings(**{**settings.__dict__, "api_key": "wrong"})) as client:
        with pytest.raises(AuthenticationError):
            await client.verify_credentials()


async def test_list_paginates_against_the_live_server(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        result = await TicketService(client).list_tickets(limit=70)

    assert result["count"] == 70
    assert result["source"] == "list"
    assert len(emulator.paths("/tickets")) >= 1
    assert result["tickets"][0]["status"] in {"open", "pending", "resolved", "closed"}


async def test_search_filters_are_honoured_by_the_server(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        result = await TicketService(client).search_tickets(status=["open"], priority=["urgent"], limit=20)

    assert result["count"] > 0
    assert {ticket["status"] for ticket in result["tickets"]} == {"open"}
    assert {ticket["priority"] for ticket in result["tickets"]} == {"urgent"}


async def test_ticket_detail_and_thread_round_trip(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        result = await TicketService(client).get_ticket(12, conversation_limit=4)

    ticket = result["ticket"]
    assert ticket["id"] == 12
    assert ticket["requester"]["name"] == "Customer 12"
    assert ticket["stats"]["first_responded_at"]
    assert len(ticket["conversation"]) == 4
    assert {entry["direction"] for entry in ticket["conversation"]} == {"inbound", "outbound"}
    assert all("[redacted-phone]" in entry["body"] for entry in ticket["conversation"])
    assert "customer12@example.com" not in json.dumps(ticket)


async def test_missing_ticket_surfaces_as_not_found(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        with pytest.raises(NotFoundError):
            await TicketService(client).get_ticket(999999)


async def test_queue_snapshot_aggregates_live_data(emulator):
    async with FreshdeskClient(live_settings(emulator)) as client:
        snapshot = await TicketService(client).queue_snapshot(window_hours=720)

    assert snapshot["sampled_tickets"] > 0
    assert set(snapshot["by_status"]) <= {"open", "pending", "waiting_on_customer", "waiting_on_third_party"}
    assert sum(snapshot["by_priority"].values()) == snapshot["sampled_tickets"]
    assert snapshot["diagnostics"]["quota"]["plan_limit"] == 1000


async def test_real_429_is_retried_and_succeeds():
    with FreshdeskEmulator(throttle_on_call=2) as emulator:
        async with FreshdeskClient(live_settings(emulator, retry_base_delay=0.01, retry_max_delay=0.05)) as client:
            await client.verify_credentials()
            result = await TicketService(client).list_tickets(limit=5)

    assert result["count"] == 5
    assert client.stats.rate_limited == 1
    assert client.stats.retries == 1
    assert client.diagnostics()["quota"]["throttled_calls"] == 1


async def test_private_note_is_written_when_enabled(emulator):
    async with FreshdeskClient(live_settings(emulator, allow_writes=True)) as client:
        result = await TicketService(client).add_private_note(12, "carrier confirmed mis-route")

    assert result["ticket_id"] == 12
    assert emulator.notes[0]["body"] == "carrier confirmed mis-route"
    assert emulator.notes[0]["private"] is True


async def test_agent_drives_the_connector_over_the_mcp_protocol(emulator):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "meridian_bridge.mcp_server"],
        env=server_env(emulator),
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            handshake = await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}

            health = _payload(await session.call_tool("health_check", {}))
            snapshot = _payload(await session.call_tool("queue_snapshot", {"window_hours": 720}))
            search = _payload(await session.call_tool("search_tickets", {"status": ["open"], "limit": 3}))
            detail = _payload(await session.call_tool("get_ticket", {"ticket_id": 5, "conversation_limit": 2}))
            denied = _payload(await session.call_tool("add_private_note", {"ticket_id": 5, "body": "x"}))
            invalid = _payload(await session.call_tool("search_tickets", {"limit": 3}))
            resource = await session.read_resource("freshdesk://ticket/5")

    assert handshake.server_info.name == "meridian-bridge"
    assert tools == {
        "health_check",
        "list_tickets",
        "get_ticket",
        "search_tickets",
        "queue_snapshot",
        "add_private_note",
    }
    assert health["ok"] is True
    assert health["identity"]["agent_id"] == 11
    assert health["config"]["allow_writes"] is False
    assert API_KEY not in json.dumps(health)
    assert snapshot["sampled_tickets"] > 0
    assert len(search["tickets"]) == 3
    assert detail["ticket"]["id"] == 5
    assert denied == {
        "ok": False,
        "error": "write_not_allowed",
        "message": "writes are disabled; set MERIDIAN_ALLOW_WRITES=true to allow private notes",
        "details": {},
    }
    assert invalid["error"] == "invalid_request"
    assert json.loads(resource.contents[0].text)["ticket"]["id"] == 5


async def test_writes_reach_freshdesk_through_the_protocol(emulator):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "meridian_bridge.mcp_server"],
        env=server_env(emulator, MERIDIAN_ALLOW_WRITES="true"),
        cwd=str(ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = _payload(await session.call_tool("add_private_note", {"ticket_id": 7, "body": "escalated"}))

    assert result["note_id"]
    assert emulator.notes[0]["ticket_id"] == 7
    assert emulator.notes[0]["body"] == "escalated"


def _payload(result) -> dict:
    return json.loads(result.content[0].text)
