"""The toolset artifact: the contract between `compile` and the runtime.

The compiler writes it, the runtime reads it, and neither needs anything else
from the other. Bump SCHEMA_VERSION on any backwards-incompatible change.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

# Tool names must be safe for both MCP clients and AgentCore Gateway targets.
TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnType(str, Enum):
    """Logical type, used to validate filter values and pick legal aggregations."""

    string = "string"
    integer = "integer"
    number = "number"
    boolean = "boolean"
    date = "date"
    timestamp = "timestamp"
    other = "other"  # selectable, but not filterable or aggregatable


class Column(_Strict):
    name: str
    type: ColumnType
    data_type: str | None = Field(None, description="Warehouse type as reported by dbt, if known.")
    description: str = ""


class Relation(_Strict):
    database: str | None = None
    schema_: str = Field(alias="schema")
    identifier: str

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelTool(_Strict):
    kind: Literal["model"] = "model"
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str
    unique_id: str = Field(description="dbt unique_id of the source model.")
    relation: Relation
    columns: list[Column] = Field(min_length=1)
    default_limit: int = Field(gt=0)
    max_limit: int = Field(gt=0)


class Source(_Strict):
    project_name: str | None = None
    dbt_version: str | None = None
    adapter_type: str | None = None


class Artifact(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    generated_at: str
    source: Source
    tools: list[ModelTool]

    def tool(self, name: str) -> ModelTool | None:
        return next((t for t in self.tools if t.name == name), None)

    def to_json(self) -> str:
        return self.model_dump_json(by_alias=True, exclude_none=True, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "Artifact":
        return cls.model_validate_json(text)
