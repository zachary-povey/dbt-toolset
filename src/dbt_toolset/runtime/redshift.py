"""Executor for Amazon Redshift (provisioned or Serverless) via the Data API."""

from __future__ import annotations

import base64
import time
from typing import Any

from .executor import ExecutionError, QueryResult


class RedshiftDataExecutor:
    def __init__(
        self,
        database: str,
        *,
        workgroup_name: str | None = None,
        cluster_identifier: str | None = None,
        secret_arn: str | None = None,
        db_user: str | None = None,
        timeout_seconds: float = 60.0,
        client: Any = None,
    ) -> None:
        if bool(workgroup_name) == bool(cluster_identifier):
            raise ValueError("set exactly one of workgroup_name (Serverless) or cluster_identifier (provisioned)")
        if secret_arn and db_user:
            raise ValueError("set at most one of secret_arn and db_user")
        self._target: dict[str, str] = {"Database": database}
        if workgroup_name:
            self._target["WorkgroupName"] = workgroup_name
        if cluster_identifier:
            self._target["ClusterIdentifier"] = cluster_identifier
        if secret_arn:
            self._target["SecretArn"] = secret_arn
        if db_user:
            self._target["DbUser"] = db_user
        self._timeout = timeout_seconds
        if client is None:
            import boto3

            client = boto3.client("redshift-data")
        self._client = client

    def execute(self, sql: str, parameters: dict[str, str]) -> QueryResult:
        request: dict[str, Any] = {"Sql": sql, **self._target}
        if parameters:
            request["Parameters"] = [{"name": k, "value": v} for k, v in parameters.items()]
        statement_id = self._client.execute_statement(**request)["Id"]
        self._wait(statement_id)
        return self._fetch(statement_id)

    def _wait(self, statement_id: str) -> None:
        deadline = time.monotonic() + self._timeout
        delay = 0.1
        while True:
            desc = self._client.describe_statement(Id=statement_id)
            status = desc["Status"]
            if status == "FINISHED":
                return
            if status in ("FAILED", "ABORTED"):
                raise ExecutionError(desc.get("Error") or f"statement {status.lower()}")
            if time.monotonic() >= deadline:
                try:
                    self._client.cancel_statement(Id=statement_id)
                except Exception:  # best effort: the statement may have just finished
                    pass
                raise ExecutionError(f"query timed out after {self._timeout:g}s")
            time.sleep(delay)
            delay = min(delay * 2, 1.0)

    def _fetch(self, statement_id: str) -> QueryResult:
        columns: list[str] = []
        rows: list[list[Any]] = []
        token: str | None = None
        while True:
            kwargs = {"Id": statement_id, **({"NextToken": token} if token else {})}
            page = self._client.get_statement_result(**kwargs)
            if not columns:
                columns = [c.get("label") or c["name"] for c in page.get("ColumnMetadata", [])]
            rows.extend([_value(f) for f in record] for record in page.get("Records", []))
            token = page.get("NextToken")
            if not token:
                return QueryResult(columns, rows)


def _value(field: dict[str, Any]) -> Any:
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in field:
            return field[key]
    if "blobValue" in field:
        return base64.b64encode(field["blobValue"]).decode()
    return None
