"""Tool-call arguments: parsing and validation against the tool's columns."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from ..artifact import Column, ColumnType, ModelTool

MAX_IN_VALUES = 1000
ALIAS_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
NUMERIC = (ColumnType.integer, ColumnType.number)


class ToolInputError(Exception):
    """The caller's arguments are invalid. The message is safe to show the agent."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Filter(_Strict):
    column: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between", "like", "is_null", "is_not_null"]
    value: Any = None


class Aggregation(_Strict):
    function: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: str | None = None
    alias: str | None = None

    @property
    def output_name(self) -> str:
        return self.alias or (f"{self.function}_{self.column}" if self.column else self.function)


class OrderBy(_Strict):
    column: str
    direction: Literal["asc", "desc"] = "asc"


class QueryRequest(_Strict):
    columns: list[str] | None = None
    filters: list[Filter] = []
    group_by: list[str] = []
    aggregations: list[Aggregation] = []
    order_by: list[OrderBy] = []
    limit: int | None = None

    @field_validator("columns", "filters", "group_by", "aggregations", "order_by", mode="before")
    @classmethod
    def _decode_json_list(cls, v: Any) -> Any:
        # Some clients (and Gateway's reduced schema) deliver lists as JSON text.
        return _maybe_json(v)

    @property
    def is_aggregate(self) -> bool:
        return bool(self.group_by or self.aggregations)

    def output_columns(self, tool: ModelTool) -> list[str]:
        if self.is_aggregate:
            return [*self.group_by, *(a.output_name for a in self.aggregations)]
        return self.columns or [c.name for c in tool.columns]


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def coerce(value: Any, column: Column) -> str:
    """Validate a filter value against the column type; return it as query-parameter text."""
    t = column.type
    try:
        if value is None:
            raise ValueError("null")
        if t is ColumnType.string:
            if isinstance(value, (dict, list, bool)):
                raise ValueError
            return str(value)
        if t is ColumnType.boolean:
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower()
            raise ValueError
        if isinstance(value, bool):
            raise ValueError
        if t is ColumnType.integer:
            d = Decimal(str(value))
            if d != d.to_integral_value():
                raise ValueError
            return str(int(d))
        if t is ColumnType.number:
            d = Decimal(str(value))
            if not d.is_finite():
                raise ValueError
            return format(d, "f")
        if t is ColumnType.date:
            return date.fromisoformat(str(value)).isoformat()
        if t is ColumnType.timestamp:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat(sep=" ")
    except (ValueError, InvalidOperation, TypeError):
        pass
    raise ToolInputError(f"filter on {column.name!r}: {value!r} is not a valid {t.value}")


def parse_request(tool: ModelTool, arguments: dict | None) -> QueryRequest:
    """Parse and fully validate a tool call. Raises ToolInputError."""
    try:
        req = QueryRequest.model_validate(arguments or {})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise ToolInputError(f"invalid arguments: {problems}") from None

    cols = {c.name: c for c in tool.columns}

    def known(name: str, where: str, usable: bool = True) -> Column:
        c = cols.get(name)
        if c is None:
            raise ToolInputError(f"{where}: unknown column {name!r}. Available: {', '.join(cols)}")
        if usable and c.type is ColumnType.other:
            raise ToolInputError(f"{where}: column {name!r} ({c.data_type}) can only be returned, not {where}")
        return c

    if req.is_aggregate and req.columns:
        raise ToolInputError("'columns' cannot be combined with group_by/aggregations; use group_by instead")
    for name in req.columns or []:
        known(name, "columns", usable=False)

    for f in req.filters:
        c = known(f.column, "filtered")
        f.value = _maybe_json(f.value) if f.op in ("in", "not_in", "between") else f.value
        if f.op in ("is_null", "is_not_null"):
            if f.value is not None:
                raise ToolInputError(f"filter on {c.name!r}: {f.op} takes no value")
        elif f.op in ("in", "not_in"):
            if not isinstance(f.value, list) or not 0 < len(f.value) <= MAX_IN_VALUES:
                raise ToolInputError(f"filter on {c.name!r}: {f.op} needs a list of 1-{MAX_IN_VALUES} values")
            f.value = [coerce(v, c) for v in f.value]
        elif f.op == "between":
            if not isinstance(f.value, list) or len(f.value) != 2:
                raise ToolInputError(f"filter on {c.name!r}: between needs a list of two values")
            f.value = [coerce(v, c) for v in f.value]
        else:
            if f.op == "like" and c.type is not ColumnType.string:
                raise ToolInputError(f"filter on {c.name!r}: like only applies to string columns")
            f.value = coerce(f.value, c)

    for name in req.group_by:
        known(name, "grouped")
    if len(set(req.group_by)) != len(req.group_by):
        raise ToolInputError("group_by lists a column twice")

    for a in req.aggregations:
        if a.column is None:
            if a.function != "count":
                raise ToolInputError(f"aggregation {a.function} needs a column")
        else:
            c = known(a.column, "aggregated")
            if a.function in ("sum", "avg") and c.type not in NUMERIC:
                raise ToolInputError(f"aggregation {a.function} needs a numeric column; {c.name!r} is {c.type.value}")
        if a.alias is not None and not ALIAS_PATTERN.fullmatch(a.alias):
            raise ToolInputError(f"alias {a.alias!r} must be letters, digits and underscores, not starting with a digit")

    out = req.output_columns(tool)
    dupes = {n for n in out if out.count(n) > 1}
    if dupes:
        raise ToolInputError(f"duplicate output column(s) {sorted(dupes)}; give aggregations distinct aliases")

    for o in req.order_by:
        if req.is_aggregate:
            if o.column not in out:
                raise ToolInputError(f"order_by: {o.column!r} is not returned. Returned: {', '.join(out)}")
        else:
            known(o.column, "ordered")

    if req.limit is not None and req.limit < 1:
        raise ToolInputError("limit must be at least 1")
    return req
