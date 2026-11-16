import json
from types import SimpleNamespace

import pytest
from mcp import Client

from dbt_toolset.runtime import lambda_handler
from dbt_toolset.runtime.executor import ExecutionError, QueryResult
from dbt_toolset.runtime.mcp_server import create_server
from dbt_toolset.runtime.toolset import Toolset


class StubExecutor:
    def __init__(self, result=None, error=None):
        self.result = result or QueryResult(["order_id"], [[1], [2]])
        self.error = error
        self.calls = []

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))
        if self.error:
            raise self.error
        return self.result


def gateway_context(tool_name):
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": tool_name}))


@pytest.fixture
def toolset(artifact):
    return Toolset(artifact, StubExecutor())


def test_lambda_dispatches_on_gateway_tool_name(toolset):
    out = lambda_handler.invoke(toolset, {"columns": ["order_id"]}, gateway_context("my-target___orders"))
    assert out == {"columns": ["order_id"], "rows": [[1], [2]], "row_count": 2, "truncated": False}


def test_lambda_returns_input_errors_to_the_agent(toolset):
    out = lambda_handler.invoke(toolset, {"columns": ["nope"]}, gateway_context("t___orders"))
    assert "unknown column 'nope'" in out["error"]


def test_lambda_cannot_reach_unexposed_models(toolset):
    assert "unknown tool" in lambda_handler.invoke(toolset, {}, gateway_context("t___secret"))["error"]


def test_lambda_reports_warehouse_errors(artifact):
    toolset = Toolset(artifact, StubExecutor(error=ExecutionError("permission denied")))
    assert lambda_handler.invoke(toolset, {}, gateway_context("t___orders")) == {
        "error": "query failed: permission denied"
    }


def test_lambda_requires_gateway_context(toolset):
    with pytest.raises(RuntimeError, match="bedrockAgentCoreToolName"):
        lambda_handler.invoke(toolset, {}, SimpleNamespace(client_context=None))


@pytest.mark.anyio
async def test_mcp_lists_and_calls_tools(toolset):
    async with Client(create_server(toolset)) as client:
        tools = (await client.list_tools()).tools
        assert {"query_customers", "orders", "revenue"} <= {t.name for t in tools}
        orders = next(t for t in tools if t.name == "orders")
        assert "One row per order." in orders.description
        assert "status" in orders.input_schema["properties"]["group_by"]["items"]["enum"]

        result = await client.call_tool("orders", {"columns": ["order_id"], "limit": 1})
        assert not result.is_error
        assert json.loads(result.content[0].text) == {
            "columns": ["order_id"], "rows": [[1]], "row_count": 1, "truncated": True,
        }  # fmt: skip

        bad = await client.call_tool("orders", {"columns": ["nope"]})
        assert bad.is_error and "unknown column" in bad.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"
