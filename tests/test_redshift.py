import pytest

from dbt_toolset.runtime.executor import ExecutionError
from dbt_toolset.runtime.redshift import RedshiftDataExecutor
from dbt_toolset.runtime.toolset import Toolset, UnknownToolError


class FakeDataApi:
    """Stands in for the boto3 redshift-data client."""

    def __init__(self, statuses=("STARTED", "FINISHED"), pages=None, error=None):
        self.statuses = list(statuses)
        self.pages = pages or [{"ColumnMetadata": [], "Records": []}]
        self.error = error
        self.executed = []
        self.cancelled = []

    def execute_statement(self, **kwargs):
        self.executed.append(kwargs)
        return {"Id": "stmt-1"}

    def describe_statement(self, Id):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"Status": status, **({"Error": self.error} if self.error else {})}

    def cancel_statement(self, Id):
        self.cancelled.append(Id)

    def get_statement_result(self, Id, NextToken=None):
        i = int(NextToken or 0)
        page = dict(self.pages[i])
        if i + 1 < len(self.pages):
            page["NextToken"] = str(i + 1)
        return page


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("dbt_toolset.runtime.redshift.time.sleep", lambda _: None)


def rows(*values):
    return [[{"longValue": v}] for v in values]


def test_executes_polls_and_pages():
    api = FakeDataApi(
        pages=[
            {"ColumnMetadata": [{"name": "n", "label": "n"}], "Records": rows(1, 2)},
            {"Records": [[{"isNull": True}], [{"stringValue": "9.50"}], [{"booleanValue": False}]]},
        ]
    )
    executor = RedshiftDataExecutor("analytics", workgroup_name="wg", client=api)
    result = executor.execute("SELECT :p1", {"p1": "x"})
    assert api.executed == [
        {"Sql": "SELECT :p1", "Database": "analytics", "WorkgroupName": "wg",
         "Parameters": [{"name": "p1", "value": "x"}]}
    ]  # fmt: skip
    assert result.columns == ["n"]
    assert result.rows == [[1], [2], [None], ["9.50"], [False]]


def test_provisioned_cluster_target_and_no_parameters():
    api = FakeDataApi()
    RedshiftDataExecutor("db", cluster_identifier="c", db_user="u", client=api).execute("SELECT 1", {})
    assert api.executed == [{"Sql": "SELECT 1", "Database": "db", "ClusterIdentifier": "c", "DbUser": "u"}]


def test_failed_statement_raises_warehouse_error():
    api = FakeDataApi(statuses=["FAILED"], error='relation "x" does not exist')
    with pytest.raises(ExecutionError, match="does not exist"):
        RedshiftDataExecutor("db", workgroup_name="wg", client=api).execute("SELECT 1", {})


def test_timeout_cancels_the_statement():
    api = FakeDataApi(statuses=["STARTED"])
    executor = RedshiftDataExecutor("db", workgroup_name="wg", timeout_seconds=0, client=api)
    with pytest.raises(ExecutionError, match="timed out"):
        executor.execute("SELECT 1", {})
    assert api.cancelled == ["stmt-1"]


@pytest.mark.parametrize("kwargs", [{}, {"workgroup_name": "w", "cluster_identifier": "c"}])
def test_requires_exactly_one_target(kwargs):
    with pytest.raises(ValueError, match="exactly one"):
        RedshiftDataExecutor("db", client=object(), **kwargs)


def test_toolset_call_truncates_to_limit(artifact):
    api = FakeDataApi(pages=[{"ColumnMetadata": [{"name": "order_id"}], "Records": rows(1, 2, 3)}])
    toolset = Toolset(artifact, RedshiftDataExecutor("analytics", workgroup_name="wg", client=api), "analytics")
    out = toolset.call("orders", {"columns": ["order_id"], "limit": 2})
    assert out == {"columns": ["order_id"], "rows": [[1], [2]], "row_count": 2, "truncated": True}
    assert api.executed[0]["Sql"] == 'SELECT "order_id" FROM "marts"."orders" LIMIT 3'


def test_toolset_names_database_only_when_it_differs(artifact):
    api = FakeDataApi()
    Toolset(artifact, RedshiftDataExecutor("other", workgroup_name="wg", client=api), "other").call("orders", {})
    assert 'FROM "analytics"."marts"."orders"' in api.executed[0]["Sql"]


def test_unknown_tool(artifact):
    with pytest.raises(UnknownToolError):
        Toolset(artifact, FakeDataApi()).call("secret", {})
