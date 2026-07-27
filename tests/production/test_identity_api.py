from __future__ import annotations

from fastapi.testclient import TestClient
from datetime import datetime, timezone
import pytest

from prd_agent.api import create_app
from prd_agent.production.dispatch import InMemoryProductionControlStore
from prd_agent.production.identity import (
    StaticBearerPrincipalResolver,
    UserPrincipal,
)
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def test_api_resolves_a_minimal_principal_for_each_authenticated_request():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    resolver = StaticBearerPrincipalResolver(
        {
            "alice-token": UserPrincipal(
                tenant_id="tenant-a",
                user_id="alice",
                roles=frozenset({"USER"}),
                scopes=frozenset({"tasks:read", "tasks:write"}),
                external_subject_hash="sha256:" + "a" * 64,
            )
        }
    )
    client = TestClient(
        create_app(repository, workflow, principal_resolver=resolver),
        raise_server_exceptions=False,
    )

    unauthorized = client.get("/api/v1/me")
    authenticated = client.get(
        "/api/v1/me",
        headers={"Authorization": "Bearer alice-token"},
    )

    assert unauthorized.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {
        "principal_type": "USER",
        "user_id": "alice",
        "roles": ["USER"],
        "scopes": ["tasks:read", "tasks:write"],
    }
    assert "tenant_id" not in authenticated.json()
    assert "external_subject_hash" not in authenticated.json()


def test_authenticated_requests_are_owner_scoped_without_global_actor_state():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    resolver = StaticBearerPrincipalResolver(
        {
            token: UserPrincipal(
                tenant_id="tenant-a",
                user_id=user_id,
                roles=frozenset({"USER"}),
                scopes=frozenset({"tasks:read", "tasks:write"}),
                external_subject_hash="sha256:" + marker * 64,
            )
            for token, user_id, marker in (
                ("alice-token", "alice", "a"),
                ("bob-token", "bob", "b"),
            )
        }
    )
    client = TestClient(
        create_app(repository, workflow, principal_resolver=resolver),
        raise_server_exceptions=False,
    )
    alice_headers = {
        "Authorization": "Bearer alice-token",
        "Idempotency-Key": "alice-start",
    }
    bob_headers = {"Authorization": "Bearer bob-token"}

    started = client.post(
        "/api/v1/tasks/from-message",
        json={"message": "订单列表增加创建时间筛选"},
        headers=alice_headers,
    )
    task_id = started.json()["task"]["task_id"]

    assert repository.get_task(task_id).owner_id == "alice"
    assert client.get("/api/v1/tasks", headers=bob_headers).json()["items"] == []
    assert client.get(
        f"/api/v1/tasks/{task_id}",
        headers=bob_headers,
    ).status_code == 404
    assert client.get(
        f"/api/v1/tasks/{task_id}",
        headers={"Authorization": "Bearer alice-token"},
    ).status_code == 200


def test_task_commands_require_write_scope_before_business_execution():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    resolver = StaticBearerPrincipalResolver(
        {
            "reader-token": UserPrincipal(
                tenant_id="tenant-a",
                user_id="reader",
                roles=frozenset({"USER"}),
                scopes=frozenset({"tasks:read"}),
                external_subject_hash="sha256:" + "c" * 64,
            )
        }
    )
    client = TestClient(
        create_app(repository, workflow, principal_resolver=resolver),
        raise_server_exceptions=False,
    )

    response = client.post(
        "/api/v1/tasks/from-message",
        json={"message": "不应执行的需求"},
        headers={
            "Authorization": "Bearer reader-token",
            "Idempotency-Key": "reader-write",
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "FORBIDDEN"
    assert repository.tasks == {}


def test_run_status_and_stop_api_are_owner_scoped_and_hide_lease_details():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(repository, HeuristicWorkflowModel())
    control = InMemoryProductionControlStore()
    resolver = StaticBearerPrincipalResolver(
        {
            token: UserPrincipal(
                tenant_id="tenant-a",
                user_id=user_id,
                roles=frozenset({"USER"}),
                scopes=frozenset({"tasks:read", "tasks:write"}),
                external_subject_hash="sha256:" + marker * 64,
            )
            for token, user_id, marker in (
                ("alice-token", "alice", "a"),
                ("bob-token", "bob", "b"),
            )
        }
    )
    client = TestClient(
        create_app(
            repository,
            workflow,
            principal_resolver=resolver,
            production_control_store=control,
        ),
        raise_server_exceptions=False,
    )
    started = client.post(
        "/api/v1/tasks/from-message",
        json={"message": "后台任务"},
        headers={
            "Authorization": "Bearer alice-token",
            "Idempotency-Key": "start-background",
        },
    ).json()
    task_id = started["task"]["task_id"]
    control.create_run_dispatch(
        run_id="production-run-1",
        task_id=task_id,
        owner_id="alice",
        reason="START_OR_RESUME",
        now=datetime.now(timezone.utc),
    )

    listed = client.get(
        f"/api/v1/tasks/{task_id}/runs",
        headers={"Authorization": "Bearer alice-token"},
    )
    forbidden = client.get(
        f"/api/v1/tasks/{task_id}/runs",
        headers={"Authorization": "Bearer bob-token"},
    )
    stopped = client.post(
        f"/api/v1/tasks/{task_id}/runs/production-run-1/stop",
        headers={
            "Authorization": "Bearer alice-token",
            "Idempotency-Key": "stop-1",
        },
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["status"] == "QUEUED"
    assert "lease_owner" not in listed.text
    assert "fencing_token" not in listed.text
    assert forbidden.status_code == 404
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPING"


def test_production_never_falls_back_to_the_local_principal(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "production")
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(repository, HeuristicWorkflowModel())

    with pytest.raises(RuntimeError, match="Production requires"):
        create_app(repository, workflow)
