"""The transport-agnostic core: an artifact plus an executor, exposed as callable tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..artifact import Artifact, MetricTool
from ..schema import Flavor, tool_definitions
from .executor import Executor
from .query import ToolInputError, parse_request
from .sql import build_query

ENV_ARTIFACT = "DBT_TOOLSET_ARTIFACT"


class UnknownToolError(ToolInputError):
    pass


class Toolset:
    def __init__(
        self,
        artifact: Artifact,
        executor: Executor,
        connection_database: str | None = None,
        metric_dialect: str = "redshift",
    ) -> None:
        self.artifact = artifact
        self._executor = executor
        self._database = (connection_database or "").lower()
        self._metric_dialect = metric_dialect
        self._engine: Any = None

    def _metricflow(self) -> Any:
        if self._engine is None:  # built on first metric call; model tools never pay for it
            from ..metricflow_engine import build_engine

            if not self.artifact.semantic_manifest:
                raise RuntimeError("the artifact has metric tools but no semantic manifest; recompile it")
            self._engine = build_engine(self.artifact.semantic_manifest, self._metric_dialect)
        return self._engine

    def definitions(self, flavor: Flavor = "mcp") -> list[dict]:
        return tool_definitions(self.artifact, flavor)

    def call(self, name: str, arguments: dict | None) -> dict[str, Any]:
        """Run a tool. Raises ToolInputError for bad calls, ExecutionError for warehouse failures."""
        tool = self.artifact.tool(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool {name!r}")
        if isinstance(tool, MetricTool):
            from .metrics import build_metric_query

            built = build_metric_query(tool, arguments, self._metricflow())
        else:
            request = parse_request(tool, arguments)
            # Name the database only when the model lives outside the one we connect to.
            cross_db = bool(tool.relation.database) and tool.relation.database.lower() != self._database
            built = build_query(tool, request, include_database=cross_db and bool(self._database))
        result = self._executor.execute(built.sql, built.parameters)
        rows = result.rows[: built.limit]
        return {
            "columns": result.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(result.rows) > built.limit,
        }


def load_artifact(path: str | Path | None = None) -> Artifact:
    return Artifact.from_json(Path(path or os.environ.get(ENV_ARTIFACT, "toolset.json")).read_text())


def toolset_from_env() -> Toolset:
    """Build a Redshift-backed toolset from DBT_TOOLSET_* environment variables."""
    from .redshift import RedshiftDataExecutor

    env = os.environ
    database = env.get("DBT_TOOLSET_REDSHIFT_DATABASE")
    if not database:
        raise RuntimeError("DBT_TOOLSET_REDSHIFT_DATABASE is not set")
    executor = RedshiftDataExecutor(
        database,
        workgroup_name=env.get("DBT_TOOLSET_REDSHIFT_WORKGROUP"),
        cluster_identifier=env.get("DBT_TOOLSET_REDSHIFT_CLUSTER"),
        secret_arn=env.get("DBT_TOOLSET_REDSHIFT_SECRET_ARN"),
        db_user=env.get("DBT_TOOLSET_REDSHIFT_DB_USER"),
        timeout_seconds=float(env.get("DBT_TOOLSET_QUERY_TIMEOUT_SECONDS", "60")),
    )
    return Toolset(load_artifact(), executor, connection_database=database)
