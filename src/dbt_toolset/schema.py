"""Tool input schemas, derived from the artifact.

Shared by the compiler (which emits them for registration) and the runtime
(which advertises them over MCP), so both always agree.
"""

from __future__ import annotations

from typing import Literal

from .artifact import Artifact, ColumnType, ModelTool

FILTER_OPS = [
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "between", "like", "is_null", "is_not_null",
]
AGG_FUNCTIONS = ["count", "count_distinct", "sum", "avg", "min", "max"]

Flavor = Literal["mcp", "gateway"]


def filterable(tool: ModelTool) -> list[str]:
    return [c.name for c in tool.columns if c.type is not ColumnType.other]


def tool_description(tool: ModelTool) -> str:
    lines = [tool.description.strip() or f"Query the {tool.relation.identifier} table.", "", "Columns:"]
    for c in tool.columns:
        doc = f" - {c.description.strip()}" if c.description.strip() else ""
        lines.append(f"- {c.name} ({c.type.value}){doc}")
    return "\n".join(lines)


def tool_input_schema(tool: ModelTool, flavor: Flavor = "mcp") -> dict:
    all_cols = [c.name for c in tool.columns]
    col = {"type": "string", "enum": all_cols}
    fcol = {"type": "string", "enum": filterable(tool)}
    schema = {
        "type": "object",
        "properties": {
            "columns": {
                "type": "array",
                "items": col,
                "description": "Columns to return. Omit for all columns. "
                "Must be omitted when group_by or aggregations is used.",
            },
            "filters": {
                "type": "array",
                "description": "Row filters, combined with AND.",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": fcol,
                        "op": {"type": "string", "enum": FILTER_OPS},
                        "value": {
                            "description": "Comparison value. A list for in/not_in, a two-item "
                            "list for between, omitted for is_null/is_not_null. "
                            "Dates and timestamps are ISO 8601 strings.",
                        },
                    },
                    "required": ["column", "op"],
                },
            },
            "group_by": {
                "type": "array",
                "items": fcol,
                "description": "Columns to group by. They are returned alongside the aggregations.",
            },
            "aggregations": {
                "type": "array",
                "description": "Aggregates to compute, per group if group_by is set, else over all rows.",
                "items": {
                    "type": "object",
                    "properties": {
                        "function": {"type": "string", "enum": AGG_FUNCTIONS},
                        "column": {**fcol, "description": "Required except for count (count of rows)."},
                        "alias": {"type": "string", "description": "Output column name."},
                    },
                    "required": ["function"],
                },
            },
            "order_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "column": {"type": "string", "description": "A returned column or aggregation alias."},
                        "direction": {"type": "string", "enum": ["asc", "desc"]},
                    },
                    "required": ["column"],
                },
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum rows to return. Default {tool.default_limit}, maximum {tool.max_limit}.",
            },
        },
    }
    return _for_gateway(schema) if flavor == "gateway" else schema


def _for_gateway(node: dict) -> dict:
    """Reduce to the keywords a Gateway Lambda target accepts.

    Gateway tool schemas take only type/properties/required/items/description,
    so enums move into the description and untyped values become strings.
    """
    out: dict = {"type": node.get("type", "string")}
    desc = node.get("description", "")
    if "enum" in node:
        desc = (desc + " " if desc else "") + "One of: " + ", ".join(node["enum"]) + "."
    if "type" not in node:
        desc = (desc + " " if desc else "") + "Pass lists as a JSON-encoded string."
    if desc:
        out["description"] = desc
    if "properties" in node:
        out["properties"] = {k: _for_gateway(v) for k, v in node["properties"].items()}
    if "required" in node:
        out["required"] = node["required"]
    if "items" in node:
        out["items"] = _for_gateway(node["items"])
    return out


def tool_definitions(artifact: Artifact, flavor: Flavor = "mcp") -> list[dict]:
    return [
        {"name": t.name, "description": tool_description(t), "inputSchema": tool_input_schema(t, flavor)}
        for t in artifact.tools
    ]
