from __future__ import annotations

import asyncio
import logging
import os
from functools import wraps
from typing import Annotated, Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from meridian_bridge import __version__
from meridian_bridge.client import FreshdeskClient
from meridian_bridge.config import Settings, load_dotenv
from meridian_bridge.errors import MeridianError
from meridian_bridge.tickets import TicketService

logger = logging.getLogger("meridian_bridge.mcp")

INSTRUCTIONS = """
Read-only access to a Freshdesk helpdesk.

Use `queue_snapshot` first to understand the shape of the queue, `search_tickets` to
narrow by status/priority/tag/date, and `get_ticket` to read one ticket with its
conversation thread. Every response carries `diagnostics` with request counts and the
remaining Freshdesk quota; stop issuing calls if `remaining` approaches zero.

Customer emails and phone numbers are masked before they reach the model. Ticket
mutations are unavailable unless the deployment explicitly enables them.
""".strip()

mcp = MCPServer("meridian-bridge", instructions=INSTRUCTIONS, version=__version__)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)

_service: TicketService | None = None
_lock = asyncio.Lock()


async def service() -> TicketService:
    global _service
    if _service is None:
        async with _lock:
            if _service is None:
                load_dotenv(os.environ.get("MERIDIAN_ENV_FILE", ".env"))
                settings = Settings.from_env()
                logger.info("connected to %s via %s", settings.base_url, settings.auth_mode)
                _service = TicketService(FreshdeskClient(settings))
    return _service


def tool(
    func: Callable[..., Awaitable[dict]] | None = None,
    *,
    annotations: ToolAnnotations = READ_ONLY,
):
    def decorate(target: Callable[..., Awaitable[dict]]) -> Callable[..., Awaitable[dict]]:
        @wraps(target)
        async def wrapper(*args: Any, **kwargs: Any) -> dict:
            try:
                return await target(*args, **kwargs)
            except MeridianError as exc:
                logger.warning("%s failed: %s", target.__name__, exc.message)
                return {"ok": False, **exc.to_dict()}

        return mcp.tool(annotations=annotations)(wrapper)

    return decorate(func) if func else decorate


@tool
async def health_check() -> dict:
    """Verify credentials and report the connector's effective configuration."""
    svc = await service()
    identity = await svc.client.verify_credentials()
    return {"ok": True, "identity": identity, "config": svc.settings.redacted(), **svc.client.diagnostics()}


@tool
async def list_tickets(
    status: Annotated[str | None, Field(description="open, pending, resolved, closed, waiting_on_customer, waiting_on_third_party")] = None,
    priority: Annotated[str | None, Field(description="low, medium, high, urgent")] = None,
    requester_email: Annotated[str | None, Field(description="Exact requester email to filter by")] = None,
    updated_since: Annotated[str | None, Field(description="ISO-8601 timestamp; only tickets updated after it")] = None,
    order_by: Annotated[str, Field(description="created_at, updated_at, due_by or status")] = "updated_at",
    order_type: Annotated[str, Field(description="asc or desc")] = "desc",
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum tickets to return")] = 25,
    include_description: Annotated[bool, Field(description="Include the sanitized first message body")] = False,
) -> dict:
    """List helpdesk tickets, newest activity first. Status/priority filters route through search."""
    svc = await service()
    return await svc.list_tickets(
        status=status,
        priority=priority,
        requester_email=requester_email,
        updated_since=updated_since,
        order_by=order_by,
        order_type=order_type,
        limit=limit,
        include_description=include_description,
    )


@tool
async def get_ticket(
    ticket_id: Annotated[int, Field(ge=1, description="Freshdesk ticket id")],
    include_conversations: Annotated[bool, Field(description="Attach the message thread")] = True,
    conversation_limit: Annotated[int, Field(ge=0, le=50, description="Maximum thread entries")] = 10,
) -> dict:
    """Read one ticket in full: fields, SLA state, requester and conversation thread."""
    svc = await service()
    return await svc.get_ticket(
        ticket_id,
        include_conversations=include_conversations,
        conversation_limit=conversation_limit,
    )


@tool
async def search_tickets(
    status: Annotated[list[str] | None, Field(description="One or more status labels, OR-ed together")] = None,
    priority: Annotated[list[str] | None, Field(description="One or more priority labels, OR-ed together")] = None,
    tags: Annotated[list[str] | None, Field(description="Freshdesk tags, OR-ed together")] = None,
    ticket_type: Annotated[str | None, Field(description="Ticket type, e.g. Question or Incident")] = None,
    agent_id: Annotated[int | None, Field(description="Assigned agent id")] = None,
    group_id: Annotated[int | None, Field(description="Helpdesk group id")] = None,
    created_after: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
    created_before: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
    updated_after: Annotated[str | None, Field(description="ISO date or timestamp")] = None,
    text: Annotated[str | None, Field(description="Keyword matched client-side against subject and description")] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Maximum tickets to return")] = 25,
    include_description: Annotated[bool, Field(description="Include sanitized ticket bodies")] = False,
) -> dict:
    """Search tickets by structured filters, optionally narrowed by a keyword. Needs at least one filter."""
    svc = await service()
    return await svc.search_tickets(
        status=status,
        priority=priority,
        tags=tags,
        ticket_type=ticket_type,
        agent_id=agent_id,
        group_id=group_id,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        text=text,
        limit=limit,
        include_description=include_description,
    )


@tool
async def queue_snapshot(
    window_hours: Annotated[int, Field(ge=1, le=720, description="Look-back window in hours")] = 24,
    group_id: Annotated[int | None, Field(description="Restrict to one helpdesk group")] = None,
) -> dict:
    """Aggregate the unresolved queue: counts by status and priority, SLA breaches, oldest tickets."""
    svc = await service()
    return await svc.queue_snapshot(window_hours=window_hours, group_id=group_id)


@tool(annotations=WRITE)
async def add_private_note(
    ticket_id: Annotated[int, Field(ge=1, description="Freshdesk ticket id")],
    body: Annotated[str, Field(min_length=1, max_length=5000, description="Internal note text")],
) -> dict:
    """Append an internal note visible only to agents. Disabled unless writes are enabled."""
    svc = await service()
    return await svc.add_private_note(ticket_id, body)


@mcp.resource("freshdesk://ticket/{ticket_id}")
async def ticket_resource(ticket_id: str) -> dict:
    """Ticket detail addressable as an MCP resource."""
    svc = await service()
    return await svc.get_ticket(int(ticket_id), conversation_limit=5)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MERIDIAN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mcp.run(transport=os.environ.get("MERIDIAN_TRANSPORT", "stdio"))


if __name__ == "__main__":
    main()
