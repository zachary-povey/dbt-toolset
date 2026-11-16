import pytest

from dbt_toolset.runtime.query import ToolInputError, parse_request
from dbt_toolset.runtime.sql import build_query


def sql(tool, args, **kw):
    built = build_query(tool, parse_request(tool, args), **kw)
    return built.sql, built.parameters


def test_default_selects_all_exposed_columns_with_default_limit(orders):
    assert sql(orders, {}) == (
        'SELECT "order_id", "customer_id", "status", "amount", "ordered_at" FROM "marts"."orders" LIMIT 51',
        {},
    )


def test_database_is_included_on_request(orders):
    assert 'FROM "analytics"."marts"."orders"' in sql(orders, {}, include_database=True)[0]


def test_filters_are_bound_and_typed(orders):
    text, params = sql(
        orders,
        {
            "columns": ["order_id"],
            "filters": [
                {"column": "status", "op": "in", "value": ["placed", "it's"]},
                {"column": "amount", "op": "gte", "value": 10.5},
                {"column": "ordered_at", "op": "between", "value": ["2024-01-01", "2024-02-01"]},
                {"column": "customer_id", "op": "ne", "value": "7"},
                {"column": "status", "op": "like", "value": "ship%"},
                {"column": "status", "op": "is_not_null"},
            ],
            "order_by": [{"column": "amount", "direction": "desc"}],
            "limit": 5,
        },
    )
    assert text == (
        'SELECT "order_id" FROM "marts"."orders" WHERE '
        '"status" IN (:p1, :p2) AND "amount" >= CAST(:p3 AS DECIMAL(38, 10)) '
        'AND "ordered_at" BETWEEN CAST(:p4 AS DATE) AND CAST(:p5 AS DATE) '
        'AND "customer_id" <> CAST(:p6 AS BIGINT) AND "status" LIKE :p7 AND NOT "status" IS NULL '
        'ORDER BY "amount" DESC NULLS LAST LIMIT 6'
    )
    assert params == {
        "p1": "placed", "p2": "it's", "p3": "10.5", "p4": "2024-01-01", "p5": "2024-02-01", "p6": "7", "p7": "ship%",
    }  # fmt: skip


def test_values_never_reach_the_sql_text(orders):
    hostile = "x'; DROP TABLE orders; --"
    text, params = sql(orders, {"filters": [{"column": "status", "op": "eq", "value": hostile}]})
    assert "DROP" not in text and params == {"p1": hostile}


def test_group_by_and_aggregations(orders):
    text, _ = sql(
        orders,
        {
            "group_by": ["status"],
            "aggregations": [
                {"function": "count"},
                {"function": "sum", "column": "amount", "alias": "total"},
                {"function": "count_distinct", "column": "customer_id"},
            ],
            "order_by": [{"column": "total", "direction": "desc"}],
        },
    )
    assert text == (
        'SELECT "status", COUNT(*) AS "count", SUM("amount") AS "total", '
        'COUNT(DISTINCT "customer_id") AS "count_distinct_customer_id" '
        'FROM "marts"."orders" GROUP BY "status" ORDER BY "total" DESC NULLS LAST LIMIT 51'
    )


def test_limit_is_capped_at_tool_maximum(orders):
    built = build_query(orders, parse_request(orders, {"limit": 10_000}))
    assert built.limit == 500 and built.sql.endswith("LIMIT 501")


def test_lists_may_arrive_json_encoded(orders):
    text, params = sql(
        orders,
        {
            "columns": '["order_id"]',
            "filters": '[{"column": "customer_id", "op": "in", "value": "[1, 2]"}]',
        },
    )
    assert '"customer_id" IN (CAST(:p1 AS BIGINT), CAST(:p2 AS BIGINT))' in text
    assert params == {"p1": "1", "p2": "2"}


@pytest.mark.parametrize(
    "args, match",
    [
        ({"columns": ["internal_note"]}, "unknown column 'internal_note'"),
        ({"columns": ['order_id" FROM x --']}, "unknown column"),
        ({"filters": [{"column": "nope", "op": "eq", "value": 1}]}, "unknown column"),
        ({"filters": [{"column": "status", "op": "regex", "value": "x"}]}, "invalid arguments"),
        ({"filters": [{"column": "customer_id", "op": "eq", "value": "1 OR 1=1"}]}, "not a valid integer"),
        ({"filters": [{"column": "customer_id", "op": "eq", "value": 1.5}]}, "not a valid integer"),
        ({"filters": [{"column": "ordered_at", "op": "gt", "value": "yesterday"}]}, "not a valid date"),
        ({"filters": [{"column": "amount", "op": "eq"}]}, "not a valid number"),
        ({"filters": [{"column": "amount", "op": "like", "value": "1%"}]}, "only applies to string"),
        ({"filters": [{"column": "status", "op": "in", "value": []}]}, "needs a list"),
        ({"filters": [{"column": "status", "op": "between", "value": ["a"]}]}, "two values"),
        ({"filters": [{"column": "status", "op": "is_null", "value": 1}]}, "takes no value"),
        ({"columns": ["status"], "group_by": ["status"]}, "cannot be combined"),
        ({"aggregations": [{"function": "sum"}]}, "needs a column"),
        ({"aggregations": [{"function": "sum", "column": "status"}]}, "numeric column"),
        ({"aggregations": [{"function": "count", "alias": 'x" --'}]}, "alias"),
        ({"aggregations": [{"function": "count"}, {"function": "count"}]}, "duplicate output"),
        ({"group_by": ["status"], "order_by": [{"column": "amount"}]}, "is not returned"),
        ({"limit": 0}, "at least 1"),
        ({"sql": "select 1"}, "invalid arguments"),
    ],
)
def test_invalid_requests_are_rejected(orders, args, match):
    with pytest.raises(ToolInputError, match=match):
        parse_request(orders, args)


def test_generated_sql_parses_as_redshift(orders):
    import sqlglot

    text, _ = sql(
        orders,
        {
            "group_by": ["status"],
            "aggregations": [{"function": "avg", "column": "amount"}],
            "filters": [{"column": "ordered_at", "op": "between", "value": ["2024-01-01", "2024-02-01"]}],
            "order_by": [{"column": "avg_amount", "direction": "desc"}],
        },
    )
    parsed = sqlglot.parse_one(text, read="redshift")
    assert isinstance(parsed, sqlglot.exp.Select) and len(parsed.expressions) == 2
