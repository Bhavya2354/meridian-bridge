from __future__ import annotations

import httpx
import pytest

from meridian_bridge import mcp_server
from meridian_bridge.config import Settings
from tests.conftest import make_service, ticket_payload

EXPECTED_TOOLS = {
    "health_check",
    "list_tickets",
    "get_ticket",
    "search_tickets",
    "queue_snapshot",
    "add_private_note",
}


@pytest.fixture
def stub_service(monkeypatch, settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/search/tickets" in request.url.path:
            return httpx.Response(200, json={"results": [ticket_payload(1)], "total": 1})
        return httpx.Response(200, json=[ticket_payload(1)])

    service = make_service(settings, handler)

    async def provider():
        return service

    monkeypatch.setattr(mcp_server, "service", provider)
    return service


async def test_every_tool_is_registered_with_a_schema():
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}

    assert EXPECTED_TOOLS <= set(tools)
    listing = tools["list_tickets"]
    assert listing.description
    assert listing.input_schema["properties"]["limit"]["maximum"] == 100
    assert "ticket_id" in tools["get_ticket"].input_schema["required"]


async def test_resource_template_is_exposed():
    templates = {template.uri_template for template in await mcp_server.mcp.list_resource_templates()}
    assert "freshdesk://ticket/{ticket_id}" in templates


async def test_tool_returns_tickets(stub_service):
    result = await mcp_server.list_tickets(limit=5)
    assert result["count"] == 1
    assert result["tickets"][0]["id"] == 1


async def test_connector_errors_come_back_as_structured_data(stub_service):
    result = await mcp_server.search_tickets(limit=5)

    assert result["ok"] is False
    assert result["error"] == "invalid_request"
    assert "structured filter" in result["message"]


async def test_write_tool_reports_disabled_state(stub_service):
    result = await mcp_server.add_private_note(1, "note")

    assert result["ok"] is False
    assert result["error"] == "write_not_allowed"
