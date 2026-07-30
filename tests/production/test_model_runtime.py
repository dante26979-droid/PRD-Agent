import httpx
from pathlib import Path
import yaml

from prd_agent.investigation.planner import ModelActionSelector
from prd_agent.production.config import DeploymentEnvironment
from prd_agent.production.model_runtime import build_model_runtime
from prd_agent.workflow.model import JsonWorkflowModelAdapter
from prd_agent.workflow.stub_model import HeuristicWorkflowModel


def test_production_runtime_uses_deepseek_for_workflow_and_loop(
    monkeypatch,
    tmp_path,
):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))

    runtime = build_model_runtime(
        DeploymentEnvironment.PRODUCTION,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500)
        ),
    )

    assert isinstance(runtime.workflow_model, JsonWorkflowModelAdapter)
    assert isinstance(runtime.action_selector, ModelActionSelector)
    assert runtime.settings is not None
    assert runtime.settings.model == "deepseek-v4-pro"
    assert runtime.investigation_budget.max_iterations == 8
    assert runtime.investigation_budget.token_budget == 24000


def test_local_runtime_remains_offline_when_llm_is_not_configured(
    monkeypatch,
):
    for name in (
        "PRD_AGENT_LLM_PROVIDER",
        "PRD_AGENT_LLM_BASE_URL",
        "PRD_AGENT_LLM_MODEL",
        "PRD_AGENT_LLM_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime = build_model_runtime(DeploymentEnvironment.LOCAL)

    assert isinstance(runtime.workflow_model, HeuristicWorkflowModel)
    assert runtime.client is None


def test_production_agent_receives_deepseek_config_and_secret_only():
    base_compose = yaml.safe_load(
        Path("infra/production/docker-compose.yml").read_text(
            encoding="utf-8"
        )
    )
    agent_compose = yaml.safe_load(
        Path("infra/production/docker-compose.go.yml").read_text(
            encoding="utf-8"
        )
    )
    services = agent_compose["services"]
    agent = services["python-agent"]

    assert agent["environment"]["PRD_AGENT_LLM_PROVIDER"] == "deepseek"
    assert (
        agent["environment"]["PRD_AGENT_LLM_API_KEY_FILE"]
        == "/run/secrets/deepseek_api_key"
    )
    assert "deepseek_api_key" in agent["secrets"]
    for service_name in ("go-api", "go-maintenance", "go-integration", "web"):
        assert "deepseek_api_key" not in services[service_name].get("secrets", [])
    assert base_compose["secrets"]["deepseek_api_key"]["file"] == (
        "./secrets/deepseek_api_key"
    )
