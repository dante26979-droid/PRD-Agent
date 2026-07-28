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


def test_production_api_receives_deepseek_config_and_secret_only():
    compose = yaml.safe_load(
        Path("infra/production/docker-compose.yml").read_text(
            encoding="utf-8"
        )
    )
    services = compose["services"]
    api = services["api"]

    assert api["environment"]["PRD_AGENT_LLM_PROVIDER"] == "deepseek"
    assert (
        api["environment"]["PRD_AGENT_LLM_API_KEY_FILE"]
        == "/run/secrets/deepseek_api_key"
    )
    assert "deepseek_api_key" in api["secrets"]
    assert "deepseek_api_key" not in services["outbox-publisher"]["secrets"]
    assert "deepseek_api_key" not in services["scheduler-reconciler"]["secrets"]
    assert compose["secrets"]["deepseek_api_key"]["file"] == (
        "./secrets/deepseek_api_key"
    )
