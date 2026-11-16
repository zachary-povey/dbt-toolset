"""AWS Lambda entry point for a Bedrock AgentCore Gateway Lambda target.

The Gateway is the MCP server. It invokes this function with the tool's
arguments as the event and the tool name in the client context, as
`<target name>___<tool name>`.

Handler: dbt_toolset.runtime.lambda_handler.handler
"""

from __future__ import annotations

import logging
from typing import Any

from .executor import ExecutionError
from .query import ToolInputError
from .toolset import Toolset, toolset_from_env

log = logging.getLogger(__name__)

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
DELIMITER = "___"

_toolset: Toolset | None = None


def tool_name_from_context(context: Any) -> str:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    name = custom.get(TOOL_NAME_KEY)
    if not name:
        raise RuntimeError(f"no {TOOL_NAME_KEY} in the client context: was this invoked by an AgentCore Gateway?")
    return name.split(DELIMITER, 1)[1] if DELIMITER in name else name


def invoke(toolset: Toolset, event: dict | None, context: Any) -> dict[str, Any]:
    name = tool_name_from_context(context)
    try:
        return toolset.call(name, event)
    except ToolInputError as e:
        # Returned, not raised, so the agent sees the message and can correct its call.
        return {"error": str(e)}
    except ExecutionError as e:
        log.warning("query failed for tool %s: %s", name, e)
        return {"error": f"query failed: {e}"}


def handler(event: dict | None, context: Any) -> dict[str, Any]:
    global _toolset
    if _toolset is None:  # reused across warm invocations
        _toolset = toolset_from_env()
    return invoke(_toolset, event, context)
