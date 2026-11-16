"""Standalone MCP server over the same toolset, for local use or non-Gateway hosting."""

from __future__ import annotations

import json
import logging
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from .executor import ExecutionError
from .query import ToolInputError
from .toolset import Toolset

log = logging.getLogger(__name__)


def create_server(toolset: Toolset, name: str = "dbt-toolset") -> Server:
    async def list_tools(_ctx: Any, _params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=d["name"],
                    description=d["description"],
                    input_schema=d["inputSchema"],
                    annotations=types.ToolAnnotations(read_only_hint=True),
                )
                for d in toolset.definitions("mcp")
            ]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # Tool errors go back in the result so the agent can read them and correct its call.
        try:
            result = toolset.call(params.name, params.arguments)
        except ToolInputError as e:
            return _text(str(e), is_error=True)
        except ExecutionError as e:
            log.warning("query failed for tool %s: %s", params.name, e)
            return _text(f"query failed: {e}", is_error=True)
        return _text(json.dumps(result, default=str))

    return Server(name, on_list_tools=list_tools, on_call_tool=call_tool)


def _text(text: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=is_error)


async def run_stdio(toolset: Toolset) -> None:
    from mcp.server.stdio import stdio_server

    server = create_server(toolset)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run_http(toolset: Toolset, host: str, port: int) -> None:
    """Streamable HTTP at /mcp. Stateless, so it can sit behind a load balancer."""
    import uvicorn

    app = create_server(toolset).streamable_http_app(json_response=True, stateless_http=True, host=host)
    uvicorn.run(app, host=host, port=port)
