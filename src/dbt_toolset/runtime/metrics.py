"""Metric tools: validate the call, then have MetricFlow render the SQL.

MetricFlow takes filters as SQL text. We never put caller values in that text:
each filter is rendered with `:pN` placeholders, which MetricFlow passes through
untouched and the executor binds.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlglot import exp

from ..artifact import Column, ColumnType, Dimension, MetricTool
from .executor import ExecutionError
from .query import Filter, OrderBy, ToolInputError, _maybe_json, validate_filter, validation_message
from .sql import DIALECT, BuiltQuery, Params, condition


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricFilter(_Strict):
    dimension: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "between", "like", "is_null", "is_not_null"]
    value: Any = None


class MetricRequest(_Strict):
    group_by: list[str] = []
    time_grain: str | None = None
    filters: list[MetricFilter] = []
    order_by: list[OrderBy] = []
    limit: int | None = None

    @field_validator("group_by", "filters", "order_by", mode="before")
    @classmethod
    def _decode_json_list(cls, v: Any) -> Any:
        return _maybe_json(v)


def _value_type(value: Any) -> ColumnType:
    """Type for a dimension whose warehouse type the compiler could not determine."""
    sample = value[0] if isinstance(value, list) and value else value
    if isinstance(sample, bool):
        return ColumnType.boolean
    if isinstance(sample, int):
        return ColumnType.integer
    if isinstance(sample, float):
        return ColumnType.number
    return ColumnType.string


def build_metric_query(tool: MetricTool, arguments: dict | None, engine: Any) -> BuiltQuery:
    """Validate a metric tool call and render its SQL. Raises ToolInputError."""
    try:
        req = MetricRequest.model_validate(arguments or {})
    except ValidationError as e:
        raise ToolInputError(validation_message(e)) from None

    dims = {d.name: d for d in tool.dimensions}

    def known(name: str, where: str) -> Dimension:
        d = dims.get(name)
        if d is None:
            raise ToolInputError(f"{where}: unknown dimension {name!r}. Available: {', '.join(dims) or 'none'}")
        return d

    if len(set(req.group_by)) != len(req.group_by):
        raise ToolInputError("group_by lists a dimension twice")

    # Output/query name of each grouped dimension: time dimensions carry their grain.
    grouped: dict[str, str] = {}
    for name in req.group_by:
        d = known(name, "group_by")
        if d.kind == "time":
            grain = req.time_grain or d.grains[0]
            if grain not in d.grains:
                raise ToolInputError(f"time_grain {grain!r} is not available for {name!r}. Available: {', '.join(d.grains)}")
            grouped[name] = f"{name}__{grain}"
        else:
            grouped[name] = name
    if req.time_grain and not any(dims[n].kind == "time" for n in req.group_by):
        raise ToolInputError("time_grain needs a time dimension in group_by")

    params = Params()
    where: list[str] = []
    for mf in req.filters:
        d = known(mf.dimension, "filters")
        f = Filter(column=d.name, op=mf.op, value=_maybe_json(mf.value) if mf.op in ("in", "not_in", "between") else mf.value)
        column = Column(name=d.name, type=d.type or _value_type(f.value))
        validate_filter(f, column)
        # MetricFlow resolves these Jinja references to the right column and join.
        ref = f"{{{{ TimeDimension('{d.name}', '{d.grains[0]}') }}}}" if d.kind == "time" else f"{{{{ Dimension('{d.name}') }}}}"
        where.append(condition(f, column, params, target=exp.Var(this=ref)).sql(dialect=DIALECT))

    order_by: list[str] = []
    for o in req.order_by:
        if o.column == tool.metric:
            target = tool.metric
        elif o.column in grouped:
            target = grouped[o.column]
        elif o.column in grouped.values():
            target = o.column
        else:
            raise ToolInputError(f"order_by: {o.column!r} is not returned. Returned: {', '.join([*grouped.values(), tool.metric])}")
        order_by.append(("-" if o.direction == "desc" else "") + target)

    if req.limit is not None and req.limit < 1:
        raise ToolInputError("limit must be at least 1")
    limit = min(req.limit or tool.default_limit, tool.max_limit)

    from metricflow.engine.metricflow_engine import MetricFlowQueryRequest
    from metricflow_semantics.errors.error_classes import InformativeUserError

    try:
        explained = engine.explain(
            MetricFlowQueryRequest.create(
                metric_names=[tool.metric],
                group_by_names=list(grouped.values()) or None,
                where_constraints=where or None,
                order_by_names=order_by or None,
                limit=limit + 1,
            )
        )
    except InformativeUserError as e:  # e.g. a dimension that cannot be combined with this metric
        raise ToolInputError(f"this combination cannot be queried: {str(e)[:800]}") from None
    except Exception as e:
        raise ExecutionError(f"could not compile the metric query: {type(e).__name__}") from e

    return BuiltQuery(sql=explained.sql_statement.without_descriptions.sql, parameters=params.values, limit=limit)
