from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest

from dbt_toolset.metricflow_engine import build_engine
from dbt_toolset.runtime.metrics import build_metric_query
from dbt_toolset.runtime.query import ToolInputError
from dbt_toolset.runtime.toolset import Toolset
from dbt_toolset.schema import tool_definitions, tool_input_schema
from test_execution import DuckDbExecutor


@pytest.fixture(scope="module")
def revenue(artifact):
    return artifact.tool("revenue")


@pytest.fixture(scope="module")
def redshift(artifact):
    return build_engine(artifact.semantic_manifest, "redshift")


# --- compile ---------------------------------------------------------------


def test_selected_metrics_become_tools(artifact):
    metrics = {t.name: t.metric for t in artifact.tools if t.kind == "metric"}
    assert metrics == {
        "revenue": "revenue",
        "order_count": "order_count",
        "average_order_value": "average_order_value",
        "running_revenue": "cumulative_revenue",  # renamed via meta
    }  # ordering_customers is excluded by the selection


def test_metric_dimensions_come_from_the_semantic_layer(revenue):
    assert [(d.name, d.kind, d.type.value if d.type else None) for d in revenue.dimensions] == [
        ("customer__region", "categorical", "string"),  # reached through a join
        ("metric_time", "time", "date"),
        ("order__ordered_at", "time", "date"),
        ("order__status", "categorical", "string"),
    ]
    assert revenue.dimensions[1].grains == ["day", "week", "month", "quarter", "year"]
    assert revenue.description == "Total order value in GBP."


def test_meta_restricts_a_metrics_dimensions(artifact):
    assert [d.name for d in artifact.tool("running_revenue").dimensions] == ["metric_time", "order__status"]


def test_metric_input_schema(revenue):
    schema = tool_input_schema(revenue)
    assert schema["properties"]["group_by"]["items"]["enum"] == [
        "customer__region", "metric_time", "order__ordered_at", "order__status",
    ]  # fmt: skip
    assert schema["properties"]["time_grain"]["enum"] == ["day", "week", "month", "quarter", "year"]


def test_metric_tools_are_in_gateway_definitions(artifact):
    names = [d["name"] for d in tool_definitions(artifact, "gateway")]
    assert {"revenue", "order_count", "average_order_value", "running_revenue"} <= set(names)


# --- SQL rendering (Redshift) ----------------------------------------------


def test_redshift_sql_binds_filter_values(revenue, redshift):
    built = build_metric_query(
        revenue,
        {
            "group_by": ["metric_time", "customer__region"],
            "time_grain": "month",
            "filters": [
                {"dimension": "order__status", "op": "in", "value": ["shipped", "x' OR '1'='1"]},
                {"dimension": "metric_time", "op": "gte", "value": "2024-01-01"},
            ],
            "order_by": [{"column": "revenue", "direction": "desc"}],
            "limit": 10,
        },
        redshift,
    )
    assert built.parameters == {"p1": "shipped", "p2": "x' OR '1'='1", "p3": "2024-01-01"}
    assert built.limit == 10
    sql = " ".join(built.sql.split())
    assert "order__status IN (:p1, :p2)" in sql
    assert "metric_time__day >= CAST(:p3 AS DATE)" in sql
    assert 'FROM "analytics"."marts"."orders"' in sql and '"analytics"."marts"."dim_customers"' in sql
    assert "DATE_TRUNC('month'" in sql and sql.endswith("ORDER BY revenue DESC LIMIT 11")
    assert "OR '1'='1" not in sql and "--" not in sql


@pytest.mark.parametrize(
    "args, match",
    [
        ({"group_by": ["order__customer_id"]}, "unknown dimension"),
        ({"group_by": ["metric_time"], "time_grain": "hour"}, "not available"),
        ({"group_by": ["order__status"], "time_grain": "month"}, "needs a time dimension"),
        ({"filters": [{"dimension": "internal_note", "op": "eq", "value": "x"}]}, "unknown dimension"),
        ({"filters": [{"dimension": "metric_time", "op": "gte", "value": "soon"}]}, "not a valid date"),
        ({"filters": [{"dimension": "order__status", "op": "eq", "value": ["a"]}]}, "not a valid string"),
        ({"order_by": [{"column": "order__status"}]}, "is not returned"),
        ({"where": "1=1"}, "invalid arguments"),
        ({"metrics": ["ordering_customers"]}, "invalid arguments"),
    ],
)
def test_invalid_metric_requests(revenue, redshift, args, match):
    with pytest.raises(ToolInputError, match=match):
        build_metric_query(revenue, args, redshift)


def test_restricted_dimension_cannot_be_used(artifact, redshift):
    with pytest.raises(ToolInputError, match="unknown dimension 'customer__region'"):
        build_metric_query(artifact.tool("running_revenue"), {"group_by": ["customer__region"]}, redshift)


# --- execution (DuckDB) ----------------------------------------------------


@pytest.fixture(scope="module")
def toolset(artifact):
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS analytics")
    con.execute("CREATE SCHEMA analytics.marts")
    con.execute(
        """
        CREATE TABLE analytics.marts.orders AS SELECT * FROM (VALUES
            (1, 10, 'placed',   9.50::DECIMAL(12,2),  DATE '2024-01-05'),
            (2, 10, 'shipped',  20.00::DECIMAL(12,2), DATE '2024-01-20'),
            (3, 11, 'shipped',  5.25::DECIMAL(12,2),  DATE '2024-02-02'),
            (4, 12, 'returned', 4.00::DECIMAL(12,2),  DATE '2024-02-10')
        ) t(order_id, customer_id, status, amount, ordered_at)
        """
    )
    con.execute(
        """
        CREATE TABLE analytics.marts.dim_customers AS SELECT * FROM (VALUES
            (10, 'emea', 2), (11, 'apac', 1), (12, 'emea', 1)
        ) t(customer_id, region, order_count)
        """
    )
    con.execute(
        "CREATE TABLE analytics.marts.metricflow_time_spine AS "
        "SELECT CAST(d AS DATE) AS date_day FROM generate_series(DATE '2024-01-01', DATE '2024-02-29', INTERVAL 1 DAY) t(d)"
    )
    return Toolset(artifact, DuckDbExecutor(con), metric_dialect="duckdb")


def test_metric_total(toolset):
    out = toolset.call("revenue", {})
    assert out["columns"] == ["revenue"] and out["rows"] == [[Decimal("38.75")]]


def test_metric_by_month_and_joined_dimension(toolset):
    out = toolset.call(
        "revenue",
        {
            "group_by": ["metric_time", "customer__region"],
            "time_grain": "month",
            "order_by": [{"column": "metric_time"}, {"column": "customer__region"}],
        },
    )
    assert out["columns"] == ["metric_time__month", "customer__region", "revenue"]
    assert [[_d(r[0]), r[1], r[2]] for r in out["rows"]] == [
        [date(2024, 1, 1), "emea", Decimal("29.50")],
        [date(2024, 2, 1), "apac", Decimal("5.25")],
        [date(2024, 2, 1), "emea", Decimal("4.00")],
    ]


def test_metric_filter_on_ungrouped_dimension(toolset):
    out = toolset.call(
        "order_count",
        {
            "filters": [
                {"dimension": "order__status", "op": "eq", "value": "shipped"},
                {"dimension": "metric_time", "op": "between", "value": ["2024-01-01", "2024-01-31"]},
            ]
        },
    )
    assert out["rows"] == [[1]]


def test_ratio_metric(toolset):
    out = toolset.call(
        "average_order_value", {"group_by": ["order__status"], "order_by": [{"column": "order__status"}]}
    )
    assert out["rows"] == [["placed", 9.5], ["returned", 4.0], ["shipped", 12.625]]


def test_cumulative_metric(toolset):
    out = toolset.call(
        "running_revenue",
        {
            "group_by": ["metric_time"],
            "filters": [{"dimension": "metric_time", "op": "in", "value": ["2024-01-05", "2024-01-31", "2024-02-29"]}],
            "order_by": [{"column": "metric_time"}],
        },
    )
    assert [r[1] for r in out["rows"]] == [Decimal("9.50"), Decimal("29.50"), Decimal("38.75")]


def test_metric_limit_and_truncation(toolset):
    out = toolset.call("revenue", {"group_by": ["order__status"], "order_by": [{"column": "revenue", "direction": "desc"}], "limit": 1})
    assert out["rows"] == [["shipped", Decimal("25.25")]] and out["truncated"]


def _d(v):
    return v.date() if isinstance(v, datetime) else v
