"""Opt-in: run model tools through the real Redshift Data API code path.

Skipped unless a target is configured. Works against real Redshift, or against a
local AWS emulator by also setting AWS_ENDPOINT_URL (boto3 reads it natively):

    DBT_TOOLSET_TEST_REDSHIFT_DATABASE=dev
    DBT_TOOLSET_TEST_REDSHIFT_WORKGROUP=...   (or DBT_TOOLSET_TEST_REDSHIFT_CLUSTER=...)
    DBT_TOOLSET_TEST_REDSHIFT_SECRET_ARN=...  (optional)   DBT_TOOLSET_TEST_REDSHIFT_DB_USER=... (optional)

It creates and drops its own schema. NOTE: written without access to either a
Redshift instance or the emulator, so it has not yet been run.
"""

import os
import uuid

import pytest

from dbt_toolset.runtime.redshift import RedshiftDataExecutor
from dbt_toolset.runtime.toolset import Toolset

ENV = "DBT_TOOLSET_TEST_REDSHIFT_"
DATABASE = os.environ.get(ENV + "DATABASE")
WORKGROUP = os.environ.get(ENV + "WORKGROUP")
CLUSTER = os.environ.get(ENV + "CLUSTER")

pytestmark = pytest.mark.skipif(
    not (DATABASE and (WORKGROUP or CLUSTER)), reason="no Redshift Data API target configured"
)


@pytest.fixture(scope="module")
def executor():
    return RedshiftDataExecutor(
        DATABASE,
        workgroup_name=WORKGROUP,
        cluster_identifier=CLUSTER,
        secret_arn=os.environ.get(ENV + "SECRET_ARN"),
        db_user=os.environ.get(ENV + "DB_USER"),
    )


@pytest.fixture(scope="module")
def toolset(artifact, executor):
    schema = f"dbt_toolset_test_{uuid.uuid4().hex[:8]}"
    executor.execute(f'CREATE SCHEMA "{schema}"', {})
    try:
        executor.execute(
            f'CREATE TABLE "{schema}"."orders" (order_id INT, customer_id INT, status VARCHAR(16), '
            "amount NUMERIC(12,2), ordered_at DATE, internal_note VARCHAR(64))",
            {},
        )
        executor.execute(
            f'INSERT INTO "{schema}"."orders" VALUES '
            "(1, 10, 'placed', 9.50, '2024-01-05', 'a'), (2, 10, 'shipped', 20.00, '2024-01-20', 'b'), "
            "(3, 11, 'shipped', 5.25, '2024-02-02', 'c'), (4, 12, 'it''s', NULL, '2024-02-10', 'd')",
            {},
        )
        patched = artifact.model_copy(deep=True)
        patched.tool("orders").relation.schema_ = schema
        yield Toolset(patched, executor, connection_database=DATABASE)
    finally:
        executor.execute(f'DROP SCHEMA "{schema}" CASCADE', {})


def test_select_with_typed_filters(toolset):
    out = toolset.call(
        "orders",
        {
            "columns": ["order_id", "amount", "ordered_at"],
            "filters": [
                {"column": "status", "op": "in", "value": ["shipped", "it's"]},
                {"column": "ordered_at", "op": "gte", "value": "2024-02-01"},
            ],
            "order_by": [{"column": "order_id"}],
        },
    )
    assert out["columns"] == ["order_id", "amount", "ordered_at"]
    assert [r[0] for r in out["rows"]] == [3, 4]
    assert out["rows"][1][1] is None


def test_group_by(toolset):
    out = toolset.call(
        "orders",
        {
            "group_by": ["status"],
            "aggregations": [{"function": "count"}, {"function": "sum", "column": "amount", "alias": "total"}],
            "filters": [{"column": "amount", "op": "is_not_null"}],
            "order_by": [{"column": "total", "direction": "desc"}],
        },
    )
    assert [r[0] for r in out["rows"]] == ["shipped", "placed"]
    assert out["rows"][0][1] == 2


def test_truncation(toolset):
    out = toolset.call("orders", {"columns": ["order_id"], "limit": 2})
    assert out["row_count"] == 2 and out["truncated"]


def test_a_named_parameter_may_be_used_twice(executor):
    # Metric SQL can repeat a filter, and so its placeholder, when MetricFlow pushes it down.
    assert executor.execute("SELECT :p1 AS a, :p1 AS b", {"p1": "x"}).rows == [["x", "x"]]
