from __future__ import annotations

import os
import uuid

from fastapi.testclient import TestClient
import pytest
import psycopg

from prd_agent.api import create_app


pytestmark = pytest.mark.skipif(
    not os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    reason="PRD_AGENT_TEST_DATABASE_DSN is required for PostgreSQL integration tests",
)


def _post(client, path, body, key):
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def test_default_api_runs_investigation_grounding_and_exposes_public_trace(
    monkeypatch,
):
    dsn = os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    monkeypatch.setenv("PRD_AGENT_DATABASE_DSN", dsn)
    app = create_app()
    suffix = uuid.uuid4().hex
    task_id = None
    with TestClient(app, raise_server_exceptions=False) as client:
        started = _post(
            client,
            "/api/v1/tasks/from-message",
            {
                "message": (
                    "为订单运营人员在订单列表增加创建时间范围筛选。"
                    "范围仅包含订单列表；开始时间不得晚于结束时间；"
                    "成功标准是准确返回时间范围内订单；"
                    "不包含历史数据迁移。"
                )
            },
            f"default-core-start-{suffix}",
        )
        assert started.status_code == 201
        detail = started.json()
        task_id = detail["task"]["task_id"]
        confirmed = _post(
            client,
            f"/api/v1/tasks/{task_id}/outline/confirm",
            {
                "outline_version": detail["outline"]["version"],
                "expected_task_version": detail["task"]["version"],
            },
            f"default-core-outline-{suffix}",
        )

        assert confirmed.status_code == 200
        detail = confirmed.json()
        assert len(detail["investigations"]) == 1
        investigation = detail["investigations"][0]
        assert investigation["status"] == "COMPLETE"
        assert investigation["stop_reason"] == "COVERAGE_COMPLETE"
        assert investigation["coverage"][0]["status"] == "COVERED"
        assert investigation["tool_calls"][0]["tool_id"] == "repo_tree"
        assert investigation["evidence"]
        assert all(
            not item["locator"].get("path", "").startswith("/")
            for item in investigation["evidence"]
        )
        assert len(detail["grounding"]) == 1
        assert detail["grounding"][0]["status"] == "PASSED"
        assert detail["grounding"][0]["confirmable"] is True

    if task_id:
        connection = psycopg.connect(dsn)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM fact_evidence_links
                 WHERE fact_id IN (
                    SELECT fact_id FROM verified_facts
                     WHERE investigation_id IN (
                        SELECT investigation_id FROM investigations
                         WHERE task_id = %s
                     )
                 )
                """,
                (task_id,),
            )
            cursor.execute(
                """
                DELETE FROM source_conflict_facts
                 WHERE conflict_id IN (
                    SELECT conflict_id FROM source_conflicts
                     WHERE investigation_id IN (
                        SELECT investigation_id FROM investigations
                         WHERE task_id = %s
                     )
                 )
                """,
                (task_id,),
            )
            for table in (
                "source_conflicts",
                "unknown_items",
                "verified_facts",
            ):
                cursor.execute(
                    f"""
                    DELETE FROM {table}
                     WHERE investigation_id IN (
                        SELECT investigation_id FROM investigations
                         WHERE task_id = %s
                     )
                    """,
                    (task_id,),
                )
            cursor.execute(
                """
                DELETE FROM source_evidence
                 WHERE tool_call_id IN (
                    SELECT tool_call_id FROM tool_calls WHERE task_id = %s
                 )
                """,
                (task_id,),
            )
            cursor.execute(
                """
                DELETE FROM tool_idempotency_records
                 WHERE tool_call_id IN (
                    SELECT tool_call_id FROM tool_calls WHERE task_id = %s
                 )
                """,
                (task_id,),
            )
            cursor.execute("DELETE FROM tool_calls WHERE task_id = %s", (task_id,))
            cursor.execute(
                """
                DELETE FROM investigation_steps
                 WHERE investigation_id IN (
                    SELECT investigation_id FROM investigations WHERE task_id = %s
                 )
                """,
                (task_id,),
            )
            cursor.execute("DELETE FROM investigations WHERE task_id = %s", (task_id,))
            for table in (
                "quality_results",
                "grounding_results",
                "prd_document_versions",
                "prd_section_versions",
                "domain_events",
                "idempotency_records",
                "agent_runs",
                "information_needs",
            ):
                cursor.execute(f"DELETE FROM {table} WHERE task_id = %s", (task_id,))
            cursor.execute(
                "DELETE FROM workflow_checkpoints WHERE task_id = %s", (task_id,)
            )
            cursor.execute(
                "DELETE FROM outline_versions WHERE task_id = %s", (task_id,)
            )
            cursor.execute(
                "DELETE FROM requirement_brief_versions WHERE task_id = %s",
                (task_id,),
            )
            cursor.execute("DELETE FROM task_messages WHERE task_id = %s", (task_id,))
            cursor.execute("DELETE FROM prd_tasks WHERE task_id = %s", (task_id,))
        connection.commit()
        connection.close()
