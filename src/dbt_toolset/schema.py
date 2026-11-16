"""Tool input schemas, derived from the artifact.

Shared by the compiler (which emits them for registration) and the runtime
(which advertises them over MCP), so both always agree.
"""

from __future__ import annotations

from typing import Literal

from .artifact import Artifact, ColumnType, MetricTool, ModelTool

FILTER_OPS = [
    "eq", "ne", "gt", "gte", "lt", "lte",
    "in", "not_in", "between", "like", "is_null", "is_not_null",
]
AGG_FUNCTIONS = ["count", "count_distinct", "sum", "avg", "min", "max"]

Flavor = Literal["mcp", "gateway"]


def filterable(tool: ModelTool) -> list[str]:
    return [c.name for c in tool.columns if c.type is not ColumnType.other]


def tool_description(tool: ModelTool | MetricTool) -> str:
    if isinstance(tool, MetricTool):
        lines = [tool.description.strip() or f"Query the {tool.metric} metric.", "", "Dimensions:"]
        for d in tool.dimensions:
            kind = f"time, grains: {', '.join(d.grains)}" if d.kind == "time" else (d.type.value if d.type else "categorical")
            doc = f" - {d.description.strip()}" if d.description.strip() else ""
            lines.append(f"- {d.name} ({kind}){doc}")
        return "\n".join(lines)
    lines = [tool.description.strip() or f"Query the {tool.relation.identifier} table.", "", "Columns:"]
    for c in tool.columns:
        doc = f" - {c.description.strip()}" if c.description.strip() else ""
        lines.append(f"- {c.name} ({c.type.value}){doc}")
    return "\n".join(lines)


_VALUE = {
    "description": "Comparison value. A list for in/not_in, a two-item list for between, "
    "omitted for is_null/is_not_null. Dates and timestamps are ISO 8601 strings.",
}


def _metric_input_schema(tool: MetricTool) -> dict:
    dim = {"type": "string", "enum": [d.name for d in tool.dimensions]}
    grains = sorted({g for d in tool.dimensions for g in d.grains}, key=lambda g: _GRAIN_ORDER.index(g))
    properties = {
        "group_by": {
            "type": "array",
            "items": dim,
            "description": f"Dimensions to break the metric down by. Omit for a single total. "
            f"Returned columns are the dimensions then '{tool.metric}'.",
        },
        "time_grain": {
            "type": "string",
            "enum": grains,
            "description": "Grain for time dimensions in group_by, e.g. month. Defaults to each dimension's finest grain. "
            "Time columns are returned as <dimension>__<grain>.",
        },
        "filters": {
            "type": "array",
            "description": "Filters applied before the metric is computed, combined with AND. "
            "Any listed dimension can be filtered, whether or not it is in group_by.",
            "items": {
                "type": "object",
                "properties": {"dimension": dim, "op": {"type": "string", "enum": FILTER_OPS}, "value": _VALUE},
                "required": ["dimension", "op"],
            },
        },
        "order_by": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "column": {"type": "string", "description": f"'{tool.metric}' or a dimension in group_by."},
                    "direction": {"type": "string", "enum": ["asc", "desc"]},
                },
                "required": ["column"],
            },
        },
        "limit": {
            "type": "integer",
            "description": f"Maximum rows to return. Default {tool.default_limit}, maximum {tool.max_limit}.",
        },
    }
    if not grains:
        del properties["time_grain"]
    return {"type": "object", "properties": properties}


_GRAIN_ORDER = ["nanosecond", "microsecond", "millisecond", "second", "minute", "hour", "day", "week", "month", "quarter", "year"]


def tool_input_schema(tool: ModelTool | MetricTool, flavor: Flavor = "mcp") -> dict:
    if isinstance(tool, MetricTool):
        schema = _metric_input_schema(tool)
        return _for_gateway(schema) if flavor == "gateway" else schema
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
                        "value": _VALUE,
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
