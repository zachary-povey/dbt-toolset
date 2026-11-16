"""MetricFlow, used only to turn metric requests into SQL: it never connects to a warehouse.

Shared by the compiler (to list each metric's dimensions) and the runtime (to render
SQL). Imports are deferred so neither needs MetricFlow unless metrics are exposed.
"""

from __future__ import annotations

import json
from typing import Any

DIALECTS = ("redshift", "duckdb")


class MetricFlowMissing(RuntimeError):
    def __init__(self) -> None:
        super().__init__("metric tools need MetricFlow: install dbt-toolset[metrics]")


def build_engine(semantic_manifest: dict[str, Any], dialect: str = "redshift"):
    try:
        from metricflow.engine.metricflow_engine import MetricFlowEngine
        from metricflow.protocols.sql_client import SqlEngine
        from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
        from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup
    except ImportError as e:
        raise MetricFlowMissing() from e

    if dialect == "redshift":
        from metricflow.sql.render.redshift import RedshiftSqlPlanRenderer as Renderer

        engine_type = SqlEngine.REDSHIFT
    elif dialect == "duckdb":
        from metricflow.sql.render.duckdb_renderer import DuckDbSqlPlanRenderer as Renderer

        engine_type = SqlEngine.DUCKDB
    else:
        raise ValueError(f"unsupported metric SQL dialect {dialect!r}; supported: {', '.join(DIALECTS)}")

    class RenderOnlyClient:
        """Satisfies MetricFlow's SqlClient protocol for rendering; refuses to run anything."""

        sql_engine_type = engine_type
        sql_plan_renderer = Renderer()

        def render_bind_parameter_key(self, key: str) -> str:
            return f":{key}"

        def _refuse(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("dbt-toolset executes SQL itself; MetricFlow only renders it")

        query = execute = dry_run = _refuse

        def close(self) -> None:
            pass

    manifest = parse_manifest_from_dbt_generated_manifest(manifest_json_string=json.dumps(semantic_manifest))
    return MetricFlowEngine(SemanticManifestLookup(manifest), RenderOnlyClient())
