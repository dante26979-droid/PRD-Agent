from __future__ import annotations

import json

from prd_agent.investigation.models import (
    CoverageItem,
    InformationNeed,
    Investigation,
    InvestigationBudget,
    InvestigationStep,
)
from prd_agent.storage.memory_investigation import InvestigationVersionConflict


def _json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _decoded(value):
    return json.loads(value) if isinstance(value, str) else value


class PostgresInvestigationStore:
    def __init__(self, connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresInvestigationStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "PostgreSQL investigation support requires: python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def save_need(self, need: InformationNeed) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO information_needs (
                    information_need_id, task_id, run_id, unit_id, trigger_stage,
                    question, requiredness, source_types_json,
                    required_coverage_json, fallback, status, planner_version,
                    context_hash, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (information_need_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    requiredness = EXCLUDED.requiredness,
                    source_types_json = EXCLUDED.source_types_json,
                    required_coverage_json = EXCLUDED.required_coverage_json
                """,
                (
                    need.information_need_id,
                    need.task_id,
                    need.run_id,
                    need.unit_id,
                    need.trigger_stage,
                    need.question,
                    need.requiredness.value,
                    _json(need.source_types),
                    _json(need.required_coverage),
                    need.fallback,
                    need.status.value,
                    need.planner_version,
                    need.context_hash,
                    need.created_at,
                ),
            )
        self.connection.commit()

    def get_need(self, information_need_id: str) -> InformationNeed:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT information_need_id, task_id, run_id, unit_id, trigger_stage,
                       question, requiredness, source_types_json,
                       required_coverage_json, fallback, status, planner_version,
                       context_hash, created_at
                  FROM information_needs WHERE information_need_id = %s
                """,
                (information_need_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError(information_need_id)
        return InformationNeed.model_validate(
            {
                "information_need_id": row[0],
                "task_id": row[1],
                "run_id": row[2],
                "unit_id": row[3],
                "trigger_stage": row[4],
                "question": row[5],
                "requiredness": row[6],
                "source_types": _decoded(row[7]),
                "required_coverage": _decoded(row[8]),
                "fallback": row[9],
                "status": row[10],
                "planner_version": row[11],
                "context_hash": row[12],
                "created_at": row[13],
            }
        )

    def create_investigation(self, investigation: Investigation) -> None:
        with self.connection.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO investigations (
                        investigation_id, information_need_id, task_id, run_id, unit_id,
                        repository_id, resolved_commit_sha, status, coverage_json,
                        budget_json, iteration_count, tool_call_count, replan_count,
                        no_progress_rounds, token_usage,
                        completed_action_signatures_json, evidence_ids_json,
                        fact_ids_json, unknown_ids_json, conflict_ids_json,
                        stop_reason, policy_version, version, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    )
                    """,
                    self._investigation_values(investigation),
                )
            except Exception:
                self.connection.rollback()
                raise
        self.connection.commit()

    def get_investigation(self, investigation_id: str) -> Investigation:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT investigation_id, information_need_id, task_id, run_id, unit_id,
                       repository_id, resolved_commit_sha, status, coverage_json,
                       budget_json, iteration_count, tool_call_count, replan_count,
                       no_progress_rounds, token_usage,
                       completed_action_signatures_json, evidence_ids_json,
                       fact_ids_json, unknown_ids_json, conflict_ids_json,
                       stop_reason, policy_version, version, created_at, updated_at
                  FROM investigations WHERE investigation_id = %s
                """,
                (investigation_id,),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError(investigation_id)
        return self._investigation_from_row(row)

    def save_investigation(
        self, investigation: Investigation, *, expected_version: int
    ) -> None:
        values = self._investigation_values(investigation)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE investigations SET
                    information_need_id = %s, task_id = %s, run_id = %s, unit_id = %s,
                    repository_id = %s, resolved_commit_sha = %s, status = %s,
                    coverage_json = %s, budget_json = %s, iteration_count = %s,
                    tool_call_count = %s, replan_count = %s, no_progress_rounds = %s,
                    token_usage = %s, completed_action_signatures_json = %s,
                    evidence_ids_json = %s, fact_ids_json = %s, unknown_ids_json = %s,
                    conflict_ids_json = %s, stop_reason = %s, policy_version = %s,
                    version = %s, created_at = %s, updated_at = %s
                 WHERE investigation_id = %s AND version = %s
                """,
                (*values[1:], investigation.investigation_id, expected_version),
            )
            if cursor.rowcount != 1:
                self.connection.rollback()
                raise InvestigationVersionConflict(
                    f"stale investigation write: {investigation.investigation_id}"
                )
        self.connection.commit()

    def append_step(self, step: InvestigationStep) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO investigation_steps (
                    investigation_step_id, investigation_id, sequence, iteration,
                    step_type, status, public_summary, input_hash, output_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    step.step_id,
                    step.investigation_id,
                    step.sequence,
                    step.iteration,
                    step.step_type,
                    step.status,
                    step.public_summary,
                    step.input_hash,
                    _json(step.output),
                    step.created_at,
                ),
            )
        self.connection.commit()

    def steps(self, investigation_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT investigation_step_id, investigation_id, sequence, iteration,
                       step_type, status, public_summary, input_hash, output_json, created_at
                  FROM investigation_steps
                 WHERE investigation_id = %s ORDER BY sequence
                """,
                (investigation_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            InvestigationStep(
                step_id=row[0],
                investigation_id=row[1],
                sequence=row[2],
                iteration=row[3],
                step_type=row[4],
                status=row[5],
                public_summary=row[6],
                input_hash=row[7],
                output=_decoded(row[8]),
                created_at=row[9],
            )
            for row in rows
        )

    def investigations_for_task(self, task_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT investigation_id, information_need_id, task_id, run_id, unit_id,
                       repository_id, resolved_commit_sha, status, coverage_json,
                       budget_json, iteration_count, tool_call_count, replan_count,
                       no_progress_rounds, token_usage,
                       completed_action_signatures_json, evidence_ids_json,
                       fact_ids_json, unknown_ids_json, conflict_ids_json,
                       stop_reason, policy_version, version, created_at, updated_at
                  FROM investigations
                 WHERE task_id = %s
                 ORDER BY created_at, investigation_id
                """,
                (task_id,),
            )
            rows = cursor.fetchall()
        return tuple(self._investigation_from_row(row) for row in rows)

    @staticmethod
    def _investigation_values(value: Investigation):
        return (
            value.investigation_id,
            value.information_need_id,
            value.task_id,
            value.run_id,
            value.unit_id,
            value.repository_id,
            value.resolved_commit_sha,
            value.status.value,
            _json({key: item.model_dump(mode="json") for key, item in value.coverage.items()}),
            _json(value.budget),
            value.iteration_count,
            value.tool_call_count,
            value.replan_count,
            value.no_progress_rounds,
            value.token_usage,
            _json(sorted(value.completed_action_signatures)),
            _json(value.evidence_ids),
            _json(value.fact_ids),
            _json(value.unknown_ids),
            _json(value.conflict_ids),
            value.stop_reason.value if value.stop_reason else None,
            value.policy_version,
            value.version,
            value.created_at,
            value.updated_at,
        )

    @staticmethod
    def _investigation_from_row(row) -> Investigation:
        raw_coverage = _decoded(row[8])
        return Investigation(
            investigation_id=row[0],
            information_need_id=row[1],
            task_id=row[2],
            run_id=row[3],
            unit_id=row[4],
            repository_id=row[5],
            resolved_commit_sha=row[6],
            status=row[7],
            coverage={
                key: CoverageItem.model_validate(item)
                for key, item in raw_coverage.items()
            },
            budget=InvestigationBudget.model_validate(_decoded(row[9])),
            iteration_count=row[10],
            tool_call_count=row[11],
            replan_count=row[12],
            no_progress_rounds=row[13],
            token_usage=row[14],
            completed_action_signatures=frozenset(_decoded(row[15])),
            evidence_ids=tuple(_decoded(row[16])),
            fact_ids=tuple(_decoded(row[17])),
            unknown_ids=tuple(_decoded(row[18])),
            conflict_ids=tuple(_decoded(row[19])),
            stop_reason=row[20],
            policy_version=row[21],
            version=row[22],
            created_at=row[23],
            updated_at=row[24],
        )
