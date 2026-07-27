from fastapi.testclient import TestClient

from prd_agent.api import create_app
from prd_agent.quality.service import DocumentQualityService
from prd_agent.repository.remote.models import RemoteRepositoryBinding
from prd_agent.repository.remote.object_reader import RemoteRepositoryCatalog
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def test_remote_repository_bindings_are_owner_scoped_and_hide_provider_identifier():
    repository = InMemoryWorkflowRepository()
    workflow = WorkflowService(
        repository,
        HeuristicWorkflowModel(),
        quality_service=DocumentQualityService(),
    )
    catalog = RemoteRepositoryCatalog(
        [
            RemoteRepositoryBinding(
                repository_id="orders",
                owner_id="local-user",
                connection_id="github-1",
                provider="GITHUB",
                provider_repository_id="private/acme-orders",
                display_name="订单服务",
                default_revision="main",
                access_scope_hash="sha256:" + "1" * 64,
            ),
            RemoteRepositoryBinding(
                repository_id="other",
                owner_id="other-user",
                connection_id="gitlab-2",
                provider="GITLAB",
                provider_repository_id="secret/other",
                display_name="其他服务",
                default_revision="main",
                access_scope_hash="sha256:" + "2" * 64,
            ),
        ]
    )
    client = TestClient(
        create_app(repository, workflow, remote_repository_catalog=catalog),
        raise_server_exceptions=False,
    )

    response = client.get("/api/v1/repository-bindings")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "repository_id": "orders",
                "provider": "GITHUB",
                "display_name": "订单服务",
                "default_revision": "main",
                "allowed_prefix": "",
            }
        ]
    }
    assert "private/acme-orders" not in response.text
    assert "secret/other" not in response.text
