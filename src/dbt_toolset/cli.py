from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .artifact import Artifact
from .schema import tool_definitions

ARTIFACT_FILE = "toolset.json"
GATEWAY_FILE = "gateway-tools.json"


def _compile(args: argparse.Namespace) -> int:
    from .compiler import CompileError, compile_project
    from .config import CompileConfig

    try:
        artifact = compile_project(
            project_dir=args.project_dir,
            config=CompileConfig.load(args.config),
            profiles_dir=args.profiles_dir,
            target=args.target,
            catalog_path=args.catalog,
        )
    except (CompileError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / ARTIFACT_FILE).write_text(artifact.to_json())
    (args.out / GATEWAY_FILE).write_text(json.dumps(tool_definitions(artifact, "gateway"), indent=2) + "\n")
    print(f"compiled {len(artifact.tools)} tool(s) to {args.out}:")
    for t in artifact.tools:
        print(f"  {t.name}  ->  {t.relation.schema_}.{t.relation.identifier}  ({len(t.columns)} columns)")
    return 0


def _json_schema(_: argparse.Namespace) -> int:
    print(json.dumps(Artifact.model_json_schema(by_alias=True), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbt-toolset")
    sub = p.add_subparsers(required=True)

    c = sub.add_parser("compile", help="compile a dbt project into a toolset artifact")
    c.add_argument("--project-dir", type=Path, default=Path("."))
    c.add_argument("--profiles-dir", type=Path)
    c.add_argument("--target")
    c.add_argument("--config", type=Path, default=Path("toolset.yml"))
    c.add_argument("--catalog", type=Path, help="catalog.json; defaults to <project>/target/catalog.json if present")
    c.add_argument("--out", type=Path, default=Path("toolset"))
    c.set_defaults(fn=_compile)

    s = sub.add_parser("json-schema", help="print the JSON Schema of the artifact format")
    s.set_defaults(fn=_json_schema)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
