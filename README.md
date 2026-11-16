# dbt-toolset

Turn the models of a dbt project into query tools for AI agents.

It has two halves, joined by one file:

1. **`compile`** (build time) reads a dbt project and writes a *toolset artifact*:
   one tool per selected model, with its table, columns, types and descriptions.
2. **The runtime** (planned) loads that artifact and serves the tools, either as an
   AWS Lambda behind an AgentCore Gateway target or as a standalone MCP server.
   It never needs dbt or the dbt project.

Deployment is out of scope: this repository is the reusable software only.

Status: the compiler and artifact format are implemented. The runtime
(SQL building, Redshift execution, Lambda and MCP front ends) and semantic-layer
metrics are not yet.

## Compile

```bash
pip install "dbt-toolset[compile]" dbt-redshift
```

```bash
dbt-toolset compile --project-dir path/to/dbt_project --config toolset.yml --out toolset/
```

Compiling parses the project and applies the selection with dbt's own logic. It
needs a valid profile but never connects to the warehouse.

`toolset.yml`:

```yaml
select: "tag:exposed"      # any dbt node selection; or `selector: <name>` from selectors.yml
exclude: "tag:pii"         # optional
tool_name_prefix: ""       # optional
limits:
  default_limit: 100       # rows returned when the caller gives no limit
  max_limit: 1000          # hard cap
```

Nothing is exposed unless selected. Outputs:

| File | Purpose |
| --- | --- |
| `toolset.json` | The artifact the runtime loads. Format: [`schema/toolset-v1.schema.json`](schema/toolset-v1.schema.json). |
| `gateway-tools.json` | Tool definitions (name, description, input schema) for registering a Gateway Lambda target. |

### Columns and types

Columns come from `catalog.json` when there is one (`dbt docs generate`; found at
`target/catalog.json` or passed with `--catalog`), otherwise from the columns
documented in the model's YAML, typed by their `data_type`. A model with no known
columns fails the compile. Columns of unrecognised type can be returned but not
filtered or aggregated.

### Per-model overrides

```yaml
models:
  - name: customers
    config:
      meta:
        dbt_toolset:
          tool_name: query_customers     # default: the model name
          description: Shown to the agent instead of the model description.
    columns:
      - name: internal_note
        meta:
          dbt_toolset:
            hidden: true                 # never exposed
```

## Tool inputs

Every model tool accepts `columns`, `filters` (`column`/`op`/`value`, ANDed),
`group_by`, `aggregations` (`count`, `count_distinct`, `sum`, `avg`, `min`, `max`),
`order_by` and `limit`. Callers never supply SQL.

## Development

```bash
uv sync
```

```bash
uv run pytest
```

After changing the artifact models, regenerate the published schema:

```bash
uv run dbt-toolset json-schema > schema/toolset-v1.schema.json
```
