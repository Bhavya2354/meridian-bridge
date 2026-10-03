from __future__ import annotations

import argparse
import asyncio
import json
import sys

from meridian_bridge.client import FreshdeskClient
from meridian_bridge.config import Settings, load_dotenv
from meridian_bridge.errors import MeridianError
from meridian_bridge.tickets import TicketService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meridian-probe",
        description="Exercise the Freshdesk connector without an MCP client.",
    )
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="verify credentials and print configuration")

    listing = sub.add_parser("list", help="list tickets")
    listing.add_argument("--status")
    listing.add_argument("--priority")
    listing.add_argument("--requester-email")
    listing.add_argument("--updated-since")
    listing.add_argument("--limit", type=int, default=10)

    get = sub.add_parser("get", help="read one ticket")
    get.add_argument("ticket_id", type=int)
    get.add_argument("--conversations", type=int, default=5)

    search = sub.add_parser("search", help="search tickets")
    search.add_argument("--status", action="append")
    search.add_argument("--priority", action="append")
    search.add_argument("--tag", action="append", dest="tags")
    search.add_argument("--text")
    search.add_argument("--limit", type=int, default=10)

    snapshot = sub.add_parser("snapshot", help="aggregate the open queue")
    snapshot.add_argument("--window-hours", type=int, default=24)
    snapshot.add_argument("--group-id", type=int)

    return parser


async def run(args: argparse.Namespace) -> dict:
    load_dotenv(args.env_file)
    settings = Settings.from_env()
    async with FreshdeskClient(settings) as client:
        service = TicketService(client)
        if args.command == "health":
            return {"identity": await client.verify_credentials(), "config": settings.redacted()}
        if args.command == "list":
            return await service.list_tickets(
                status=args.status,
                priority=args.priority,
                requester_email=args.requester_email,
                updated_since=args.updated_since,
                limit=args.limit,
            )
        if args.command == "get":
            return await service.get_ticket(args.ticket_id, conversation_limit=args.conversations)
        if args.command == "search":
            return await service.search_tickets(
                status=args.status,
                priority=args.priority,
                tags=args.tags,
                text=args.text,
                limit=args.limit,
            )
        return await service.queue_snapshot(window_hours=args.window_hours, group_id=args.group_id)


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except MeridianError as exc:
        print(json.dumps(exc.to_dict(), indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
