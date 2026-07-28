from __future__ import annotations

from dataclasses import dataclass

import httpx

from prd_agent.application.default_investigation import (
    RepositoryStructureSelector,
)
from prd_agent.investigation.models import InvestigationBudget
from prd_agent.investigation.planner import ModelActionSelector
from prd_agent.model_api.deepseek import DeepSeekChatClient
from prd_agent.workflow.model import JsonWorkflowModelAdapter
from prd_agent.workflow.stub_model import HeuristicWorkflowModel

from .config import (
    DeploymentEnvironment,
    LlmSettings,
    llm_settings,
)


@dataclass
class ModelRuntime:
    workflow_model: object
    action_selector: object
    investigation_budget: InvestigationBudget
    settings: LlmSettings | None = None
    client: DeepSeekChatClient | None = None

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


def build_model_runtime(
    environment: DeploymentEnvironment,
    *,
    transport: httpx.BaseTransport | None = None,
) -> ModelRuntime:
    settings = llm_settings(environment)
    if settings is None:
        return ModelRuntime(
            workflow_model=HeuristicWorkflowModel(),
            action_selector=RepositoryStructureSelector(),
            investigation_budget=InvestigationBudget(),
        )
    client = DeepSeekChatClient(settings, transport=transport)
    workflow_model = JsonWorkflowModelAdapter(
        client,
        timeout_seconds=settings.timeout_seconds,
    )
    return ModelRuntime(
        workflow_model=workflow_model,
        action_selector=ModelActionSelector(workflow_model),
        investigation_budget=InvestigationBudget(
            max_iterations=settings.max_iterations,
            token_budget=settings.run_token_budget,
        ),
        settings=settings,
        client=client,
    )
