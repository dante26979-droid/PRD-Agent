from __future__ import annotations

from fastapi.testclient import TestClient

from prd_agent.api import create_app
from prd_agent.domain.commands import StartTask
from prd_agent.model_api.errors import ModelApiError
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def client_bundle():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    app = create_app(repository, workflow)
    return TestClient(app, raise_server_exceptions=False), repository, workflow


def post(client, path, body, key):
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def test_health_and_empty_task_list():
    client, _, _ = client_bundle()

    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    assert client.get("/api/v1/health/ready").json() == {"status": "ready"}
    response = client.get("/api/v1/tasks")

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}
    assert response.headers["x-request-id"].startswith("req-")


def test_model_provider_failure_is_a_redacted_retryable_503():
    class FailingModel:
        def complete(self, operation, payload, *, repair=False):
            raise ModelApiError(
                "transport_timeout",
                retryable=True,
            )

    repository = InMemoryWorkflowRepository()
    app = create_app(
        repository,
        WorkflowService(repository, FailingModel()),
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "订单列表增加创建时间筛选"},
        "model-provider-failure",
    )

    assert response.status_code == 503
    assert response.json()["error_code"] == "MODEL_PROVIDER_ERROR"
    assert response.json()["retryable"] is True
    assert "transport_timeout" not in str(response.json())


def test_complete_prd_through_http_commands():
    client, _, _ = client_bundle()

    started = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "订单列表增加创建时间筛选"},
        "web-start-1",
    )
    assert started.status_code == 201
    detail = started.json()
    task_id = detail["task"]["task_id"]
    assert detail["task"]["display_status"] == "AWAITING_CONFIRMATION"
    assert [item["type"] for item in detail["available_actions"]] == [
        "SEND_MESSAGE",
        "CONFIRM_OUTLINE",
    ]

    confirmed_outline = post(
        client,
        f"/api/v1/tasks/{task_id}/outline/confirm",
        {
            "outline_version": detail["outline"]["version"],
            "expected_task_version": detail["task"]["version"],
        },
        "web-outline-1",
    )
    assert confirmed_outline.status_code == 200
    detail = confirmed_outline.json()
    action = detail["available_actions"][0]
    assert action["type"] == "CONFIRM_UNIT"

    confirmed_unit = post(
        client,
        f"/api/v1/tasks/{task_id}/units/{action['target_id']}/confirm",
        {"expected_task_version": detail["task"]["version"]},
        "web-unit-1",
    )
    assert confirmed_unit.status_code == 200
    detail = confirmed_unit.json()
    assert detail["document"]["markdown"].startswith("# ")
    assert detail["quality"][-1]["confirmable"] is True
    assert detail["quality"][-1]["issues"] == []
    assert detail["available_actions"][0]["type"] == "FINALIZE_PRD"

    action = detail["available_actions"][0]
    finalized = post(
        client,
        f"/api/v1/tasks/{task_id}/finalize",
        {
            **action["payload"],
            "expected_task_version": detail["task"]["version"],
        },
        "web-finalize-1",
    )
    assert finalized.status_code == 200
    detail = finalized.json()
    assert detail["task"]["task_status"] == "COMPLETED"
    assert detail["available_actions"][0]["type"] == "REOPEN_PRD"


def test_idempotency_replay_and_version_conflict_use_public_errors():
    client, repository, _ = client_bundle()
    response = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "订单列表增加创建时间筛选"},
        "same-key",
    )
    task_id = response.json()["task"]["task_id"]

    replay = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "订单列表增加创建时间筛选"},
        "same-key",
    )
    conflict = post(
        client,
        f"/api/v1/tasks/{task_id}/outline/confirm",
        {"outline_version": 1, "expected_task_version": 999},
        "stale-outline",
    )

    assert replay.status_code == 201
    assert replay.json()["task"]["task_id"] == task_id
    assert len(repository.tasks) == 1
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "TASK_VERSION_CONFLICT"
    assert "999" not in conflict.json()["message"]


def test_owner_mismatch_is_indistinguishable_from_missing_task():
    client, _, workflow = client_bundle()
    other = workflow.start_task(
        StartTask(
            "其他用户的需求",
            "other-start",
            actor_id="other-user",
        )
    )

    forbidden = client.get(f"/api/v1/tasks/{other.task.task_id}")
    missing = client.get("/api/v1/tasks/task-does-not-exist")

    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json()["error_code"] == missing.json()["error_code"]
    assert other.task.task_id not in str(forbidden.json())


def test_list_is_owner_scoped_and_cursor_paginated():
    client, _, workflow = client_bundle()
    for index in range(3):
        workflow.start_task(
            StartTask(
                f"需求 {index}",
                f"local-{index}",
            )
        )
    workflow.start_task(
        StartTask("其他用户需求", "other-list", actor_id="other-user")
    )

    first = client.get("/api/v1/tasks?limit=2").json()
    second = client.get(
        "/api/v1/tasks",
        params={"limit": 2, "cursor": first["next_cursor"]},
    ).json()

    task_ids = [item["task_id"] for item in first["items"] + second["items"]]
    assert len(task_ids) == len(set(task_ids)) == 3
    assert first["next_cursor"]
    assert second["next_cursor"] is None


def test_request_actor_cannot_be_forged_and_unknown_fields_are_rejected():
    client, _, _ = client_bundle()

    response = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "合法需求", "actor_id": "other-user"},
        "forged",
    )

    assert response.status_code == 422
    assert response.json()["error_code"] == "VALIDATION_ERROR"


def test_openapi_contains_step7_contract():
    client, _, _ = client_bundle()

    schema = client.get("/openapi.json").json()

    assert "/api/v1/tasks" in schema["paths"]
    assert "/api/v1/tasks/{task_id}/events" in schema["paths"]
    assert "TaskDetailResponse" in schema["components"]["schemas"]
