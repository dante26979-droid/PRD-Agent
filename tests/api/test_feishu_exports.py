from __future__ import annotations

from fastapi.testclient import TestClient

from prd_agent.api import create_app
from prd_agent.export.models import (
    CreateIdempotencyCapability,
    ProviderDocumentResult,
)
from prd_agent.export.service import ExportApplicationService
from prd_agent.export.source import WorkflowExportDocumentSource
from prd_agent.export.store import InMemoryExportStore
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


class FakeFeishuGateway:
    create_idempotency_capability = CreateIdempotencyCapability.PROVIDER_KEY

    def __init__(self) -> None:
        self.created = []

    def create_document(self, document, *, idempotency_key):
        self.created.append(document)
        return ProviderDocumentResult(
            external_id="private-document-token",
            safe_url="https://example.feishu.cn/docx/safe",
            title=document.title,
            provider_revision="revision-1",
        )

    def overwrite_document(self, *args, **kwargs):
        raise AssertionError("not used")


def post(client, path, body, key):
    return client.post(path, json=body, headers={"Idempotency-Key": key})


def completed_task(client):
    detail = post(
        client,
        "/api/v1/tasks/from-message",
        {"message": "订单列表增加创建时间筛选"},
        "start-export-task",
    ).json()
    task_id = detail["task"]["task_id"]
    detail = post(
        client,
        f"/api/v1/tasks/{task_id}/outline/confirm",
        {
            "outline_version": detail["outline"]["version"],
            "expected_task_version": detail["task"]["version"],
        },
        "confirm-export-outline",
    ).json()
    action = detail["available_actions"][0]
    detail = post(
        client,
        f"/api/v1/tasks/{task_id}/units/{action['target_id']}/confirm",
        {"expected_task_version": detail["task"]["version"]},
        "confirm-export-unit",
    ).json()
    action = detail["available_actions"][0]
    detail = post(
        client,
        f"/api/v1/tasks/{task_id}/finalize",
        {
            **action["payload"],
            "expected_task_version": detail["task"]["version"],
        },
        "finalize-export-task",
    ).json()
    return task_id, detail


def test_completed_task_can_create_feishu_export_without_exposing_external_id():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    gateway = FakeFeishuGateway()
    exports = ExportApplicationService(
        WorkflowExportDocumentSource(repository),
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"api-confirmation-secret",
    )
    client = TestClient(
        create_app(repository, workflow, export_service=exports),
        raise_server_exceptions=False,
    )
    task_id, detail = completed_task(client)

    preview = post(
        client,
        f"/api/v1/tasks/{task_id}/exports/feishu/preview",
        {
            "mode": "CREATE",
            "expected_task_version": detail["task"]["version"],
        },
        "preview-export",
    )
    assert preview.status_code == 200
    value = preview.json()
    exported = post(
        client,
        f"/api/v1/tasks/{task_id}/exports/feishu",
        {
            "intent_id": value["intent_id"],
            "confirmation_token": value["confirmation_token"],
            "mode": "CREATE",
            "expected_task_version": detail["task"]["version"],
        },
        "execute-export",
    )
    history = client.get(f"/api/v1/tasks/{task_id}/exports")

    assert exported.status_code == 200
    assert exported.json()["status"] == "SUCCEEDED"
    assert history.status_code == 200
    assert history.json()["items"][0]["safe_url"].endswith("/docx/safe")
    assert "private-document-token" not in exported.text
    assert "private-document-token" not in history.text
    assert len(gateway.created) == 1


def test_export_intent_cannot_be_executed_through_a_different_task_endpoint():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    gateway = FakeFeishuGateway()
    exports = ExportApplicationService(
        WorkflowExportDocumentSource(repository),
        InMemoryExportStore(),
        gateway,
        confirmation_secret=b"api-confirmation-secret",
    )
    client = TestClient(
        create_app(repository, workflow, export_service=exports),
        raise_server_exceptions=False,
    )
    task_id, detail = completed_task(client)
    preview = post(
        client,
        f"/api/v1/tasks/{task_id}/exports/feishu/preview",
        {
            "mode": "CREATE",
            "expected_task_version": detail["task"]["version"],
        },
        "preview-wrong-target",
    ).json()

    response = post(
        client,
        "/api/v1/tasks/task-other/exports/feishu",
        {
            "intent_id": preview["intent_id"],
            "confirmation_token": preview["confirmation_token"],
            "mode": "CREATE",
            "expected_task_version": detail["task"]["version"],
        },
        "execute-wrong-target",
    )

    assert response.status_code == 404
    assert gateway.created == []


def test_default_local_configuration_enables_feishu_export_preview(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_FEISHU_APP_ID", "cli_test")
    monkeypatch.setenv("PRD_AGENT_FEISHU_APP_SECRET", "test-app-secret")
    monkeypatch.setenv(
        "PRD_AGENT_FEISHU_DOCUMENT_HOST",
        "example.feishu.cn",
    )
    monkeypatch.setenv("PRD_AGENT_FEISHU_WIKI_NODE_TOKEN", "wiki-node-1")
    monkeypatch.setenv(
        "PRD_AGENT_EXPORT_CONFIRMATION_SECRET",
        "test-confirmation-secret",
    )
    monkeypatch.setenv(
        "PRD_AGENT_EXTERNAL_ID_ENCRYPTION_KEY",
        "test-encryption-secret",
    )
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    client = TestClient(
        create_app(repository, workflow),
        raise_server_exceptions=False,
    )
    task_id, detail = completed_task(client)

    preview = post(
        client,
        f"/api/v1/tasks/{task_id}/exports/feishu/preview",
        {
            "mode": "CREATE",
            "expected_task_version": detail["task"]["version"],
        },
        "preview-default-feishu",
    )

    assert preview.status_code == 200
    assert preview.json()["title"]
