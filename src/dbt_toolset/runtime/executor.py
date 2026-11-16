"""The warehouse boundary. Implement `Executor` to support another warehouse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class ExecutionError(Exception):
    """The warehouse rejected or failed the query."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]


class Executor(Protocol):
    def execute(self, sql: str, parameters: dict[str, str]) -> QueryResult:
        """Run `sql` with named parameters (`:name` placeholders, text values)."""
        ...
