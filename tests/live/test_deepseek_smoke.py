import json
import os

import pytest

from prd_agent.domain.commands import StartTask
from prd_agent.domain.enums import TaskStatus
from prd_agent.investigation.models import InformationNeed
from prd_agent.investigation.planner import ModelActionSelector
from prd_agent.model_api.deepseek import DeepSeekChatClient
from prd_agent.production.config import (
    DeploymentEnvironment,
    llm_settings,
)
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.model import JsonWorkflowModelAdapter
from prd_agent.workflow.service import WorkflowService

from tests.investigation.support import build_services
from tests.repository.support import TemporaryGitRepository


pytestmark = pytest.mark.live_llm


def _client() -> DeepSeekChatClient:
    if os.environ.get("PRD_AGENT_LIVE_LLM_TEST") != "1":
        pytest.skip("set PRD_AGENT_LIVE_LLM_TEST=1 to enable live LLM tests")
    settings = llm_settings(DeploymentEnvironment.LOCAL)
    if settings is None:
        pytest.skip("live LLM settings are not configured")
    return DeepSeekChatClient(settings)


def test_configured_model_is_available():
    client = _client()
    try:
        assert client.settings.model in client.list_models()
    finally:
        client.close()


def test_deepseek_returns_minimal_json_output():
    client = _client()
    try:
        response = client.complete(
            "只返回 JSON 对象，不得输出其他内容。",
            '输出 {"status":"ok","value":2}，其中 value 为 1+1。',
            timeout_seconds=30,
        )
        output = json.loads(response.output)
        assert output == {"status": "ok", "value": 2}
        assert int(response.token_usage.get("total_tokens", 0)) > 0
    finally:
        client.close()


def test_deepseek_drives_a_real_repository_tool_loop():
    client = _client()
    fixture = TemporaryGitRepository()
    try:
        fixture.write("demo/src/orders.py", "class OrderService:\n    pass\n")
        fixture.commit()
        selector = ModelActionSelector(JsonWorkflowModelAdapter(client))
        snapshot, evidence_store, _, application = build_services(
            fixture,
            selector,
        )
        need = InformationNeed(
            information_need_id="need-live-deepseek-loop",
            question="定位当前仓库的主要代码结构。",
            requiredness="REQUIRED",
            source_types=("CODE",),
            required_coverage=("repository_structure",),
            trigger_stage="LIVE_SMOKE",
            fallback="MARK_UNKNOWN",
            planner_version="live.v1",
            context_hash="sha256:live",
        )
        investigation = application.create(
            need,
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        result = application.run(investigation.investigation_id)

        assert result.status.value == "COMPLETE"
        assert len(evidence_store.all_calls()) == 1
    finally:
        fixture.cleanup()
        client.close()


def test_deepseek_drives_start_task_to_outline_review():
    client = _client()
    try:
        workflow = WorkflowService(
            InMemoryWorkflowRepository(),
            JsonWorkflowModelAdapter(client),
        )

        snapshot = workflow.start_task(
            StartTask(
                message=(
                    "为订单运营人员在订单列表增加创建时间范围筛选。"
                    "范围仅包含订单列表；开始时间不得晚于结束时间；"
                    "成功标准是准确返回时间范围内订单；不包含历史数据迁移。"
                ),
                idempotency_key="live-deepseek-start-task",
            )
        )

        assert snapshot.task.status == TaskStatus.OUTLINE_REVIEW
        assert snapshot.outlines
    finally:
        client.close()
