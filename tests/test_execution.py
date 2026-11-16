"""Run generated SQL for real, against DuckDB, to check the results and not just the text.

DuckDB is not Redshift, but it accepts this SQL unchanged apart from parameter syntax,
and it doubles as an example of supporting another warehouse via the Executor protocol.
"""

import re
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from dbt_toolset.runtime.executor import ExecutionError, QueryResult
from dbt_toolset.runtime.toolset import Toolset


class DuckDbExecutor:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters):
        try:
            cursor = self.connection.execute(re.sub(r":(p\d+)\b", r"$\1", sql), parameters)
        except duckdb.Error as e:
            raise ExecutionError(str(e)) from e
        return QueryResult([d[0] for d in cursor.description], [list(r) for r in cursor.fetchall()])


@pytest.fixture(scope="module")
def toolset(artifact):
    con = duckdb.connect()
    con.execute("CREATE SCHEMA marts")
    con.execute(
        """
        CREATE TABLE marts.orders AS SELECT * FROM (VALUES
            (1, 10, 'placed',   9.50::DECIMAL(12,2),  DATE '2024-01-05', 'secret a'),
            (2, 10, 'shipped',  20.00::DECIMAL(12,2), DATE '2024-01-20', 'secret b'),
            (3, 11, 'shipped',  5.25::DECIMAL(12,2),  DATE '2024-02-02', 'secret c'),
            (4, 12, 'returned', NULL,                 DATE '2024-02-10', 'secret d'),
            (5, 12, 'it''s',    1.00::DECIMAL(12,2),  DATE '2024-03-01', 'secret e')
        ) t(order_id, customer_id, status, amount, ordered_at, internal_note)
        """
    )
    return Toolset(artifact, DuckDbExecutor(con))


def test_select_all_never_returns_hidden_columns(toolset):
    out = toolset.call("orders", {})
    assert out["columns"] == ["order_id", "customer_id", "status", "amount", "ordered_at"]
    assert out["row_count"] == 5 and not out["truncated"]


def test_filters(toolset):
    out = toolset.call(
        "orders",
        {
            "columns": ["order_id"],
            "filters": [
                {"column": "status", "op": "in", "value": ["shipped", "it's"]},
                {"column": "amount", "op": "lt", "value": 20},
                {"column": "ordered_at", "op": "between", "value": ["2024-02-01", "2024-12-31"]},
            ],
            "order_by": [{"column": "order_id"}],
        },
    )
    assert out["rows"] == [[3], [5]]


def test_null_and_like_filters(toolset):
    assert toolset.call(
        "orders", {"columns": ["order_id"], "filters": [{"column": "amount", "op": "is_null"}]}
    )["rows"] == [[4]]
    assert toolset.call(
        "orders",
        {"columns": ["order_id"], "filters": [{"column": "status", "op": "like", "value": "ship%"}],
         "order_by": [{"column": "order_id", "direction": "desc"}]},
    )["rows"] == [[3], [2]]  # fmt: skip


def test_group_by_with_aggregations(toolset):
    out = toolset.call(
        "orders",
        {
            "group_by": ["status"],
            "aggregations": [
                {"function": "count"},
                {"function": "sum", "column": "amount", "alias": "total"},
                {"function": "count_distinct", "column": "customer_id", "alias": "customers"},
            ],
            "filters": [{"column": "ordered_at", "op": "lt", "value": "2024-03-01"}],
            "order_by": [{"column": "total", "direction": "desc"}],
        },
    )
    assert out["columns"] == ["status", "count", "total", "customers"]
    # NULL totals sort last when descending.
    assert out["rows"] == [["shipped", 2, Decimal("25.25"), 2], ["placed", 1, Decimal("9.50"), 1], ["returned", 1, None, 1]]


def test_aggregation_without_group_by(toolset):
    out = toolset.call("orders", {"aggregations": [{"function": "max", "column": "ordered_at"}]})
    assert out["rows"] == [[date(2024, 3, 1)]]


def test_limit_and_truncation(toolset):
    out = toolset.call("orders", {"columns": ["order_id"], "order_by": [{"column": "order_id"}], "limit": 2})
    assert out["rows"] == [[1], [2]] and out["truncated"]


def test_hostile_value_is_just_a_value(toolset):
    out = toolset.call(
        "orders", {"filters": [{"column": "status", "op": "eq", "value": "x' OR '1'='1"}]}
    )
    assert out["row_count"] == 0
