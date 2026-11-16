"""Build Redshift SQL for a validated request.

Identifiers come only from the artifact and are always quoted. Caller-supplied
values never enter the SQL text: they are bound as named parameters.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from ..artifact import Column, ColumnType, ModelTool
from .query import Aggregation, Filter, QueryRequest

DIALECT = "redshift"


@dataclass
class BuiltQuery:
    sql: str
    parameters: dict[str, str]
    limit: int  # rows the caller gets; the SQL asks for one more to detect truncation


def _col(name: str) -> exp.Column:
    return exp.column(name, quoted=True)


def _cast_type(c: Column) -> str | None:
    dt = (c.data_type or "").lower()
    match c.type:
        case ColumnType.integer:
            return "BIGINT"
        case ColumnType.number:
            return "DOUBLE PRECISION" if dt.startswith(("real", "float", "double")) else "DECIMAL(38, 10)"
        case ColumnType.boolean:
            return "BOOLEAN"
        case ColumnType.date:
            return "DATE"
        case ColumnType.timestamp:
            return "TIMESTAMPTZ" if "tz" in dt or "with time zone" in dt else "TIMESTAMP"
    return None  # strings bind as-is


class _Params:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def bind(self, value: str, column: Column) -> exp.Expression:
        name = f"p{len(self.values) + 1}"
        self.values[name] = value
        placeholder = exp.Var(this=f":{name}")  # the Data API's named-parameter syntax
        to = _cast_type(column)
        return exp.cast(placeholder, to, dialect=DIALECT) if to else placeholder


def _condition(f: Filter, column: Column, params: _Params) -> exp.Expression:
    col = _col(column.name)
    match f.op:
        case "is_null":
            return col.is_(exp.null())
        case "is_not_null":
            return exp.not_(col.is_(exp.null()))
        case "in":
            return col.isin(*(params.bind(v, column) for v in f.value))
        case "not_in":
            return exp.not_(col.isin(*(params.bind(v, column) for v in f.value)))
        case "between":
            low, high = (params.bind(v, column) for v in f.value)
            return exp.Between(this=col, low=low, high=high)
        case "like":
            return col.like(params.bind(f.value, column))
    op = {"eq": exp.EQ, "ne": exp.NEQ, "gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}[f.op]
    return op(this=col, expression=params.bind(f.value, column))


def _aggregate(a: Aggregation) -> exp.Expression:
    if a.column is None:
        agg: exp.Expression = exp.Count(this=exp.Star())
    elif a.function == "count_distinct":
        agg = exp.Count(this=exp.Distinct(expressions=[_col(a.column)]))
    else:
        fn = {"count": exp.Count, "sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max}[a.function]
        agg = fn(this=_col(a.column))
    return exp.alias_(agg, a.output_name, quoted=True)


def build_query(tool: ModelTool, req: QueryRequest, include_database: bool = False) -> BuiltQuery:
    """`req` must come from parse_request, which has already validated it against `tool`."""
    cols = {c.name: c for c in tool.columns}
    params = _Params()
    rel = tool.relation
    table = exp.table_(
        rel.identifier, db=rel.schema_, catalog=rel.database if include_database else None, quoted=True
    )

    if req.is_aggregate:
        projections = [*(_col(g) for g in req.group_by), *(_aggregate(a) for a in req.aggregations)]
    else:
        projections = [_col(n) for n in req.output_columns(tool)]

    query = exp.select(*projections).from_(table)
    if req.filters:
        query = query.where(exp.and_(*(_condition(f, cols[f.column], params) for f in req.filters), wrap=False))
    if req.group_by:
        query = query.group_by(*(_col(g) for g in req.group_by))
    for o in req.order_by:
        query = query.order_by(exp.Ordered(this=_col(o.column), desc=o.direction == "desc"))

    limit = min(req.limit or tool.default_limit, tool.max_limit)
    query = query.limit(limit + 1)
    return BuiltQuery(sql=query.sql(dialect=DIALECT), parameters=params.values, limit=limit)
