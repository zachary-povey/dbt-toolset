"""Compile a dbt project into a toolset artifact.

`build_artifact` is pure (dicts in, Artifact out). `resolve_selection` is the
only part that imports dbt, so the runtime never needs dbt installed.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .artifact import Artifact, Column, ColumnType, Dimension, MetricTool, ModelTool, Relation, Source
from .config import CompileConfig, Selection

META_KEY = "dbt_toolset"


class CompileError(Exception):
    pass


_TYPE_PREFIXES: list[tuple[ColumnType, tuple[str, ...]]] = [
    (ColumnType.boolean, ("bool",)),
    (ColumnType.timestamp, ("timestamp",)),
    (ColumnType.date, ("date",)),
    (ColumnType.integer, ("smallint", "integer", "bigint", "int")),
    (ColumnType.number, ("numeric", "decimal", "real", "double", "float")),
    (ColumnType.string, ("char", "varchar", "nchar", "nvarchar", "bpchar", "text", "string", "character")),
]


def logical_type(data_type: str | None) -> ColumnType:
    """Map a warehouse type name (Redshift naming) to a logical type."""
    if not data_type:
        return ColumnType.other
    t = data_type.strip().lower()
    for logical, prefixes in _TYPE_PREFIXES:
        if t.startswith(prefixes):
            return logical
    return ColumnType.other


def _meta(obj: dict) -> dict:
    meta = {**(obj.get("config") or {}).get("meta", {}), **(obj.get("meta") or {})}
    return meta.get(META_KEY) or {}


def _columns(node: dict, catalog_node: dict | None, include_hidden: bool = False) -> list[Column]:
    documented = {name.lower(): c for name, c in (node.get("columns") or {}).items()}
    if catalog_node:
        cat_cols = sorted(catalog_node["columns"].values(), key=lambda c: c.get("index", 0))
        names_types = [(c["name"], c.get("type")) for c in cat_cols]
    else:
        names_types = [(c["name"], c.get("data_type")) for c in documented.values()]

    out = []
    for name, data_type in names_types:
        doc = documented.get(name.lower(), {})
        if _meta(doc).get("hidden") and not include_hidden:
            continue
        out.append(
            Column(
                name=name,
                type=logical_type(data_type),
                data_type=data_type,
                description=doc.get("description") or "",
            )
        )
    return out


def _tool_name(node: dict, prefix: str) -> str:
    name = _meta(node).get("tool_name") or prefix + re.sub(r"[^A-Za-z0-9_-]", "_", node["name"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise CompileError(
            f"{node['unique_id']}: tool name {name!r} must be 1-64 characters of letters, digits, _ or -; "
            f"set meta.{META_KEY}.tool_name"
        )
    return name


TIME_GRAINS = ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]


def _dimension_type(manifest: dict, catalog_nodes: dict, semantic_model: str | None, dimension: str) -> ColumnType | None:
    """Best-effort type of a categorical dimension, from the column of the model behind it."""
    for sm in manifest.get("semantic_models", {}).values():
        if sm["name"] != semantic_model:
            continue
        expr = next((d.get("expr") or d["name"] for d in sm.get("dimensions", []) if d["name"] == dimension), None)
        for uid in (sm.get("depends_on") or {}).get("nodes", []):
            node = manifest["nodes"].get(uid)
            if node and expr:
                for c in _columns(node, catalog_nodes.get(uid), include_hidden=True):
                    if c.name.lower() == expr.lower() and c.type is not ColumnType.other:
                        return c.type
    return None


def _metric_tools(
    manifest: dict, semantic_manifest: dict, metric_ids: list[str], config: CompileConfig, catalog_nodes: dict
) -> list[MetricTool]:
    from .metricflow_engine import build_engine

    engine = build_engine(semantic_manifest)
    prefix = config.metrics.tool_name_prefix if config.metrics else ""
    tools = []
    for uid in sorted(metric_ids):
        metric = manifest.get("metrics", {}).get(uid)
        if metric is None:
            raise CompileError(f"{uid}: not a metric in the manifest")
        meta = _meta(metric)
        allow, deny = meta.get("dimensions"), set(meta.get("exclude_dimensions") or [])

        dimensions = []
        for d in engine.list_dimensions(metric_names=[metric["name"]]):
            is_time = d.type.value == "time"
            name = "metric_time" if d.name == "metric_time" else d.dunder_name
            if (allow is not None and name not in allow) or name in deny:
                continue
            if is_time:
                base = d.type_params.time_granularity.value if d.type_params else "day"
                grains = TIME_GRAINS[TIME_GRAINS.index(base) :] if base in TIME_GRAINS else [base]
                dtype = ColumnType.date if base in TIME_GRAINS[3:] else ColumnType.timestamp
            else:
                grains = []
                model = d.semantic_model_reference.semantic_model_name if d.semantic_model_reference else None
                dtype = _dimension_type(manifest, catalog_nodes, model, d.name)
            dimensions.append(
                Dimension(
                    name=name,
                    kind="time" if is_time else "categorical",
                    type=dtype,
                    description=d.description or "",
                    grains=grains,
                )
            )
        unknown = set(allow or []) - {d.name for d in dimensions} - deny
        if unknown:
            raise CompileError(f"{uid}: meta.{META_KEY}.dimensions lists {sorted(unknown)}, not available for this metric")

        try:  # fail the build, not the first agent call, if the metric cannot be rendered
            from metricflow.engine.metricflow_engine import MetricFlowQueryRequest

            engine.explain(MetricFlowQueryRequest.create(metric_names=[metric["name"]]))
        except Exception as e:
            raise CompileError(f"{uid}: MetricFlow cannot compile this metric: {e}") from e

        tools.append(
            MetricTool(
                name=_tool_name(metric, prefix),
                description=meta.get("description") or metric.get("description") or metric.get("label") or "",
                unique_id=uid,
                metric=metric["name"],
                dimensions=dimensions,
                default_limit=config.limits.default_limit,
                max_limit=config.limits.max_limit,
            )
        )
    return tools


def build_artifact(
    manifest: dict,
    unique_ids: list[str],
    config: CompileConfig,
    catalog: dict | None = None,
    metric_ids: list[str] | None = None,
    semantic_manifest: dict | None = None,
) -> Artifact:
    tools: list[ModelTool | MetricTool] = []
    seen: dict[str, str] = {}
    catalog_nodes = (catalog or {}).get("nodes", {})

    for uid in sorted(unique_ids):
        node = manifest["nodes"].get(uid)
        if node is None or node.get("resource_type") != "model":
            raise CompileError(f"{uid}: not a model in the manifest")
        if (node.get("config") or {}).get("materialized") == "ephemeral":
            raise CompileError(f"{uid}: ephemeral models have no table to query; exclude it from the selection")

        columns = _columns(node, catalog_nodes.get(uid))
        if not columns:
            raise CompileError(
                f"{uid}: no columns known. Document its columns in YAML, or run `dbt docs generate` "
                "and pass the catalog"
            )

        name = _tool_name(node, config.tool_name_prefix)
        if name in seen:
            raise CompileError(f"tool name {name!r} is produced by both {seen[name]} and {uid}")
        seen[name] = uid

        tools.append(
            ModelTool(
                name=name,
                description=_meta(node).get("description") or node.get("description") or "",
                unique_id=uid,
                relation=Relation(
                    database=node.get("database"),
                    schema=node["schema"],
                    identifier=node.get("alias") or node["name"],
                ),
                columns=columns,
                default_limit=config.limits.default_limit,
                max_limit=config.limits.max_limit,
            )
        )

    if not tools:
        raise CompileError("the selection matched no models")

    if config.metrics:
        if not metric_ids:
            raise CompileError("the metrics selection matched no metrics")
        if not semantic_manifest:
            raise CompileError("metrics are selected but dbt produced no semantic_manifest.json")
        for tool in _metric_tools(manifest, semantic_manifest, metric_ids, config, catalog_nodes):
            if tool.name in seen:
                raise CompileError(
                    f"tool name {tool.name!r} is produced by both {seen[tool.name]} and {tool.unique_id}; "
                    f"set metrics.tool_name_prefix or meta.{META_KEY}.tool_name"
                )
            seen[tool.name] = tool.unique_id
            tools.append(tool)

    md = manifest.get("metadata", {})
    return Artifact(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=Source(
            project_name=md.get("project_name"),
            dbt_version=md.get("dbt_version"),
            adapter_type=md.get("adapter_type"),
        ),
        tools=tools,
        semantic_manifest=semantic_manifest if config.metrics else None,
    )


def _dbt_ls(base_args: list[str], resource_type: str, selection: Selection) -> list[str]:
    from dbt.cli.main import dbtRunner

    args = [*base_args, "--resource-type", resource_type, "--output", "json", "--output-keys", "unique_id"]
    if selection.selector:
        args += ["--selector", selection.selector]
    if selection.select:
        args += ["--select", *selection.select.split()]
    if selection.exclude:
        args += ["--exclude", *selection.exclude.split()]
    with contextlib.redirect_stdout(io.StringIO()):  # dbt echoes the selection
        res = dbtRunner().invoke(args)
    if not res.success:
        raise CompileError(f"dbt ls failed: {res.exception or 'see dbt output above'}")
    return [json.loads(line)["unique_id"] for line in (res.result or [])]


def compile_project(
    project_dir: Path,
    config: CompileConfig,
    profiles_dir: Path | None = None,
    target: str | None = None,
    catalog_path: Path | None = None,
) -> Artifact:
    """Parse the project with dbt, apply the selections with dbt's own logic, build the artifact.

    Needs a valid profile but no warehouse connection.
    """
    try:
        import dbt.cli.main  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise CompileError("compiling needs dbt: install dbt-toolset[compile] and your dbt adapter") from e

    with tempfile.TemporaryDirectory() as tmp:
        base = ["--quiet", "--log-path", tmp, "ls", "--project-dir", str(project_dir), "--target-path", tmp]
        if profiles_dir:
            base += ["--profiles-dir", str(profiles_dir)]
        if target:
            base += ["--target", target]

        model_ids = _dbt_ls(base, "model", config)
        metric_ids = _dbt_ls(base, "metric", config.metrics) if config.metrics else None
        manifest = json.loads((Path(tmp) / "manifest.json").read_text())
        sm_path = Path(tmp) / "semantic_manifest.json"
        semantic_manifest = json.loads(sm_path.read_text()) if config.metrics and sm_path.exists() else None

    if catalog_path is None:
        default = project_dir / "target" / "catalog.json"
        catalog_path = default if default.exists() else None
    catalog = json.loads(catalog_path.read_text()) if catalog_path else None
    return build_artifact(manifest, model_ids, config, catalog, metric_ids, semantic_manifest)
