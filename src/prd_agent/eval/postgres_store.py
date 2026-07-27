"""PostgreSQL-backed RunStore.

`psycopg` is intentionally optional for the offline unit-test path. Production
or local PostgreSQL execution must install the `postgres` extra.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .models import BaselineRun


class Connection(Protocol):
    def cursor(self):
        ...

    def commit(self) -> None:
        ...


class PostgresRunStore:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresRunStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError(
                "PostgreSQL support requires: python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def get(self, eval_run_id: str) -> BaselineRun | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT eval_run_id, case_id, config_id, trial_no, model_id,
                       prompt_version, dataset_version, repository_commit,
                       input_hash, output_hash, started_at, duration_ms,
                       token_usage, metadata, status, output, error
                  FROM eval_run WHERE eval_run_id = %s
                """,
                (eval_run_id,),
            )
            row = cursor.fetchone()
        return self._from_row(row) if row else None

    def save(self, run: BaselineRun) -> None:
        existing = self.get(run.eval_run_id)
        if existing:
            if existing.input_hash != run.input_hash:
                raise ValueError(f"run id collision with different input: {run.eval_run_id}")
            return
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO eval_run (
                    eval_run_id, case_id, config_id, trial_no, model_id,
                    prompt_version, dataset_version, repository_commit,
                    input_hash, output_hash, started_at, duration_ms,
                    token_usage, metadata, status, output, error
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run.eval_run_id,
                    run.case_id,
                    run.config_id,
                    run.trial_no,
                    run.model_id,
                    run.prompt_version,
                    run.dataset_version,
                    run.repository_commit,
                    run.input_hash,
                    run.output_hash,
                    run.started_at,
                    run.duration_ms,
                    json.dumps(dict(run.token_usage), ensure_ascii=False),
                    json.dumps(dict(run.metadata), ensure_ascii=False),
                    run.status,
                    run.output,
                    run.error,
                ),
            )
        self.connection.commit()

    def all(self) -> list[BaselineRun]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT eval_run_id, case_id, config_id, trial_no, model_id,
                       prompt_version, dataset_version, repository_commit,
                       input_hash, output_hash, started_at, duration_ms,
                       token_usage, metadata, status, output, error
                  FROM eval_run ORDER BY started_at, eval_run_id
                """
            )
            rows = cursor.fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: Any) -> BaselineRun:
        token_usage = row[12]
        if isinstance(token_usage, str):
            token_usage = json.loads(token_usage)
        metadata = row[13]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return BaselineRun(
            eval_run_id=row[0],
            case_id=row[1],
            config_id=row[2],
            trial_no=row[3],
            model_id=row[4],
            prompt_version=row[5],
            dataset_version=row[6],
            repository_commit=row[7],
            input_hash=row[8],
            output_hash=row[9],
            started_at=row[10],
            duration_ms=row[11],
            token_usage=token_usage or {},
            metadata=metadata or {},
            status=row[14],
            output=row[15],
            error=row[16],
        )
