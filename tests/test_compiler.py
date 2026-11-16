import json
from pathlib import Path

import pytest

from dbt_toolset.artifact import Artifact, ColumnType
from dbt_toolset.cli import main
from dbt_toolset.compiler import CompileError, build_artifact, logical_type
from dbt_toolset.config import CompileConfig
from dbt_toolset.schema import tool_definitions, tool_input_schema

PROJECT = Path(__file__).parent / "fixtures" / "sample_project"
REPO = Path(__file__).parent.parent


def test_selector_limits_exposed_models(artifact):
    models = {t.unique_id for t in artifact.tools if t.kind == "model"}
    assert models == {"model.sample.orders", "model.sample.customers"}


def test_model_becomes_tool(artifact):
    orders = artifact.tool("orders")
    assert orders.description == "One row per order."
    assert (orders.relation.database, orders.relation.schema_, orders.relation.identifier) == (
        "analytics", "marts", "orders",
    )  # fmt: skip
    assert (orders.default_limit, orders.max_limit) == (50, 500)
    assert [(c.name, c.type) for c in orders.columns] == [
        ("order_id", ColumnType.integer),
        ("customer_id", ColumnType.integer),
        ("status", ColumnType.string),
        ("amount", ColumnType.number),
        ("ordered_at", ColumnType.date),
    ]


def test_hidden_column_is_dropped(artifact):
    assert "internal_note" not in [c.name for c in artifact.tool("orders").columns]


def test_meta_tool_name_and_alias(artifact):
    customers = artifact.tool("query_customers")
    assert customers.relation.identifier == "dim_customers"


def test_artifact_round_trips(artifact):
    assert Artifact.from_json(artifact.to_json()) == artifact


def test_catalog_supplies_columns_and_types(artifact):
    manifest = {
        "metadata": {},
        "nodes": {
            "model.p.m": {
                "unique_id": "model.p.m", "resource_type": "model", "name": "m", "schema": "s",
                "columns": {"B": {"name": "B", "description": "documented"}},
            }
        },
    }  # fmt: skip
    catalog = {
        "nodes": {
            "model.p.m": {
                "columns": {
                    "b": {"name": "b", "type": "timestamp without time zone", "index": 2},
                    "a": {"name": "a", "type": "super", "index": 1},
                }
            }
        }
    }
    tool = build_artifact(manifest, ["model.p.m"], CompileConfig(select="m"), catalog).tools[0]
    assert [(c.name, c.type, c.description) for c in tool.columns] == [
        ("a", ColumnType.other, ""),
        ("b", ColumnType.timestamp, "documented"),
    ]


def _node(**over):
    return {"unique_id": "model.p.m", "resource_type": "model", "name": "m", "schema": "s",
            "columns": {"a": {"name": "a", "data_type": "int"}}, **over}  # fmt: skip


@pytest.mark.parametrize(
    "nodes, match",
    [
        ({"model.p.m": _node(columns={})}, "no columns known"),
        ({"model.p.m": _node(config={"materialized": "ephemeral"})}, "ephemeral"),
        ({"model.p.m": _node(name="x" * 65)}, "tool name"),
        (
            {"model.p.m": _node(), "model.p.n": _node(unique_id="model.p.n", meta={"dbt_toolset": {"tool_name": "m"}})},
            "produced by both",
        ),
        ({}, "matched no models"),
    ],
)
def test_compile_errors(nodes, match):
    with pytest.raises(CompileError, match=match):
        build_artifact({"nodes": nodes}, list(nodes), CompileConfig(select="x"))


def test_config_requires_a_selection():
    with pytest.raises(ValueError, match="nothing is exposed by default"):
        CompileConfig()


@pytest.mark.parametrize(
    "data_type, expected",
    [
        ("character varying(256)", "string"), ("BIGINT", "integer"), ("double precision", "number"),
        ("numeric(18,2)", "number"), ("boolean", "boolean"), ("timestamp with time zone", "timestamp"),
        ("date", "date"), ("super", "other"), (None, "other"),
    ],
)  # fmt: skip
def test_logical_type(data_type, expected):
    assert logical_type(data_type).value == expected


def test_input_schema_only_offers_known_columns(artifact):
    schema = tool_input_schema(artifact.tool("orders"))
    assert schema["properties"]["columns"]["items"]["enum"] == [
        "order_id", "customer_id", "status", "amount", "ordered_at",
    ]  # fmt: skip


def test_gateway_schema_uses_only_supported_keywords(artifact):
    allowed = {"type", "properties", "required", "items", "description"}

    def walk(node):
        assert set(node) <= allowed and "type" in node, node
        for child in node.get("properties", {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    for definition in tool_definitions(artifact, "gateway"):
        walk(definition["inputSchema"])
    orders = next(d for d in tool_definitions(artifact, "gateway") if d["name"] == "orders")
    assert "order_id" in orders["inputSchema"]["properties"]["columns"]["items"]["description"]


def test_cli_writes_both_outputs(tmp_path, capsys):
    rc = main(["compile", "--project-dir", str(PROJECT), "--profiles-dir", str(PROJECT),
               "--config", str(PROJECT / "toolset.yml"), "--out", str(tmp_path)])  # fmt: skip
    assert rc == 0
    assert Artifact.from_json((tmp_path / "toolset.json").read_text()).tool("orders")
    assert [t["name"] for t in json.loads((tmp_path / "gateway-tools.json").read_text())] == [
        "query_customers", "orders", "average_order_value", "running_revenue", "order_count", "revenue",
    ]  # fmt: skip
    assert "unique_id" not in capsys.readouterr().out


def test_published_json_schema_is_current():
    published = json.loads((REPO / "schema" / "toolset-v1.schema.json").read_text())
    assert published == Artifact.model_json_schema(by_alias=True)
