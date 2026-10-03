from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from meridian_bridge import __version__  # noqa: E402
from meridian_bridge.mcp_server import INSTRUCTIONS, mcp  # noqa: E402

OUTPUT = ROOT / "docs" / "mcp-tools.json"


async def build_spec() -> dict:
    tools = await mcp.list_tools()
    resources = await mcp.list_resource_templates()
    return {
        "name": "meridian-bridge",
        "version": __version__,
        "protocol": "mcp",
        "transport": ["stdio", "sse", "streamable-http"],
        "instructions": INSTRUCTIONS,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else None,
            }
            for tool in tools
        ],
        "resources": [
            {
                "uriTemplate": template.uri_template,
                "name": template.name,
                "description": template.description,
            }
            for template in resources
        ],
    }


def main() -> None:
    spec = asyncio.run(build_spec())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} with {len(spec['tools'])} tools")


if __name__ == "__main__":
    main()
