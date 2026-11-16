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

from .artifact import Artifact, Column, ColumnType, ModelTool, Relation, Source
from .config import CompileConfig

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


def _columns(node: dict, catalog_node: dict | None) -> list[Column]:
    documented = {name.lower(): c for name, c in (node.get("columns") or {}).items()}
    if catalog_node:
        cat_cols = sorted(catalog_node["columns"].values(), key=lambda c: c.get("index", 0))
        names_types = [(c["name"], c.get("type")) for c in cat_cols]
    else:
        names_types = [(c["name"], c.get("data_type")) for c in documented.values()]

    out = []
    for name, data_type in names_types:
        doc = documented.get(name.lower(), {})
        if _meta(doc).get("hidden"):
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


def build_artifact(
    manifest: dict,
    unique_ids: list[str],
    config: CompileConfig,
    catalog: dict | None = None,
) -> Artifact:
    tools: list[ModelTool] = []
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

    md = manifest.get("metadata", {})
    return Artifact(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=Source(
            project_name=md.get("project_name"),
            dbt_version=md.get("dbt_version"),
            adapter_type=md.get("adapter_type"),
        ),
        tools=tools,
    )


def resolve_selection(
    project_dir: Path,
    config: CompileConfig,
    profiles_dir: Path | None = None,
    target: str | None = None,
) -> tuple[dict, list[str]]:
    """Parse the project with dbt and apply the selection with dbt's own logic.

    Returns (manifest, selected model unique_ids). Needs no warehouse connection.
    """
    try:
        from dbt.cli.main import dbtRunner
    except ImportError as e:  # pragma: no cover
        raise CompileError("compiling needs dbt: install dbt-toolset[compile] and your dbt adapter") from e

    with tempfile.TemporaryDirectory() as tmp:
        args = [
            "--quiet", "--log-path", tmp,
            "ls",
            "--project-dir", str(project_dir),
            "--target-path", tmp,
            "--resource-type", "model",
            "--output", "json", "--output-keys", "unique_id",
        ]  # fmt: skip
        if profiles_dir:
            args += ["--profiles-dir", str(profiles_dir)]
        if target:
            args += ["--target", target]
        if config.selector:
            args += ["--selector", config.selector]
        if config.select:
            args += ["--select", *config.select.split()]
        if config.exclude:
            args += ["--exclude", *config.exclude.split()]

        with contextlib.redirect_stdout(io.StringIO()):  # dbt echoes the selection
            res = dbtRunner().invoke(args)
        if not res.success:
            raise CompileError(f"dbt ls failed: {res.exception or 'see dbt output above'}")
        unique_ids = [json.loads(line)["unique_id"] for line in (res.result or [])]
        manifest = json.loads((Path(tmp) / "manifest.json").read_text())
    return manifest, unique_ids


def compile_project(
    project_dir: Path,
    config: CompileConfig,
    profiles_dir: Path | None = None,
    target: str | None = None,
    catalog_path: Path | None = None,
) -> Artifact:
    manifest, unique_ids = resolve_selection(project_dir, config, profiles_dir, target)
    if catalog_path is None:
        default = project_dir / "target" / "catalog.json"
        catalog_path = default if default.exists() else None
    catalog = json.loads(catalog_path.read_text()) if catalog_path else None
    return build_artifact(manifest, unique_ids, config, catalog)
