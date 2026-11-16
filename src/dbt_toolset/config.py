"""Compile-time configuration (the user's toolset.yml)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_limit: int = Field(100, gt=0)
    max_limit: int = Field(1000, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "Limits":
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit must not exceed max_limit")
        return self


class CompileConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    select: str | None = Field(None, description="dbt node selection, e.g. 'tag:exposed'.")
    exclude: str | None = None
    selector: str | None = Field(None, description="Name of a YAML selector in selectors.yml.")
    tool_name_prefix: str = ""
    limits: Limits = Limits()

    @model_validator(mode="after")
    def _one_selection(self) -> "CompileConfig":
        if self.selector and (self.select or self.exclude):
            raise ValueError("use either 'selector' or 'select'/'exclude', not both")
        if not (self.selector or self.select):
            raise ValueError("a 'select' or 'selector' is required: nothing is exposed by default")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "CompileConfig":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})
