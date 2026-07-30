import json

import httpx
import pytest

from prd_agent.investigation.models import InformationNeed, ProposedAction
from prd_agent.investigation.planner import ModelActionSelector
from prd_agent.model_api.deepseek import DeepSeekChatClient
from prd_agent.model_api.errors import ModelApiError
from prd_agent.production.config import LlmSettings
from prd_agent.workflow.model import JsonWorkflowModelAdapter

from tests.investigation.support import build_services
from tests.repository.support import TemporaryGitRepository


def test_deepseek_selector_uses_first_tool_result_in_second_loop_round(
    tmp_path,
):
    fixture = TemporaryGitRepository()
    try:
        fixture.write(
            "demo/db/schema.sql",
            "CREATE TABLE orders (amount NUMERIC(12, 2) NOT NULL);\n",
        )
        fixture.write(
            "demo/src/rules.py",
            "def valid_amount(amount):\n    return amount > 0\n",
        )
        fixture.commit()
        secret = tmp_path / "deepseek_api_key"
        secret.write_text("test-secret\n", encoding="utf-8")
        actions = iter(
            [
                ProposedAction(
                    tool_id="parse_database_schema",
                    arguments={"path": "db/schema.sql", "table": "orders"},
                    purpose="确认订单金额存储格式",
                    target_coverage=("storage_schema",),
                ).model_dump(mode="json"),
                ProposedAction(
                    tool_id="find_symbol",
                    arguments={"symbol": "valid_amount"},
                    purpose="定位金额校验逻辑",
                    target_coverage=("validation_logic",),
                ).model_dump(mode="json"),
            ]
        )
        model_payloads = []
        system_prompts = []

        def handler(request: httpx.Request) -> httpx.Response:
            request_body = json.loads(request.content)
            system_prompts.append(
                request_body["messages"][0]["content"]
            )
            model_payloads.append(
                json.loads(request_body["messages"][1]["content"])
            )
            return httpx.Response(
                200,
                json={
                    "model": "deepseek-v4-pro",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    next(actions),
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ],
                    "usage": {"total_tokens": 20},
                },
            )

        client = DeepSeekChatClient(
            LlmSettings(
                provider="deepseek",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-pro",
                api_key_file=secret,
            ),
            transport=httpx.MockTransport(handler),
        )
        selector = ModelActionSelector(JsonWorkflowModelAdapter(client))
        snapshot, evidence_store, store, application = build_services(
            fixture,
            selector,
        )
        need = InformationNeed(
            information_need_id="need-llm-loop",
            question="订单金额如何存储和校验？",
            requiredness="REQUIRED",
            source_types=("CODE",),
            required_coverage=("storage_schema", "validation_logic"),
            trigger_stage="UNIT_PREPARATION",
            fallback="ASK_USER_OR_MARK_UNKNOWN",
            planner_version="v1",
            context_hash="sha256:test",
        )
        investigation = application.create(
            need,
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        result = application.run(investigation.investigation_id)

        assert result.status.value == "COMPLETE", (
            result,
            store.steps(investigation.investigation_id),
        )
        assert len(model_payloads) == 2
        assert '"tool_id"' in system_prompts[0]
        assert '"target_coverage"' in system_prompts[0]
        assert len(evidence_store.all_calls()) == 2
        assert any(
            step["step_type"] == "EXECUTE_TOOL"
            and "数据库 Schema Parser" in step["summary"]
            for step in model_payloads[1]["prior_public_steps"]
        )
        first_tool_result = model_payloads[1]["prior_tool_results"][0]
        assert first_tool_result["tool_id"] == "parse_database_schema"
        assert first_tool_result["status"] == "SUCCEEDED"
        assert first_tool_result["evidence_ids"]
        model_steps = [
            step
            for step in store.steps(investigation.investigation_id)
            if step.step_type == "CALL_MODEL"
        ]
        assert len(model_steps) == 2
        assert model_steps[0].output["model_id"] == "deepseek-v4-pro"
        assert model_steps[0].output["prompt_version"].endswith(
            ".m0.step2.v1"
        )
        assert model_steps[0].output["raw_output_hash"].startswith(
            "sha256:"
        )
        assert model_steps[0].output["token_usage"]["total_tokens"] == 20
        assert "test-secret" not in str(model_steps)
        assert (
            store.get_investigation(
                investigation.investigation_id
            ).token_usage
            == 40
        )
    finally:
        fixture.cleanup()


def test_provider_outage_fails_investigation_without_calling_tool():
    fixture = TemporaryGitRepository()
    try:
        fixture.write("demo/src/orders.py", "class OrderService:\n    pass\n")
        fixture.commit()

        class FailingWorkflowModel:
            def complete(self, operation, payload, *, repair=False):
                raise ModelApiError(
                    "retry_exhausted",
                    retryable=True,
                )

        selector = ModelActionSelector(FailingWorkflowModel())
        snapshot, evidence_store, store, application = build_services(
            fixture,
            selector,
        )
        need = InformationNeed(
            information_need_id="need-provider-outage",
            question="定位仓库结构",
            requiredness="REQUIRED",
            source_types=("CODE",),
            required_coverage=("repository_structure",),
            trigger_stage="UNIT_PREPARATION",
            fallback="MARK_UNKNOWN",
            planner_version="v1",
            context_hash="sha256:test",
        )
        investigation = application.create(
            need,
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        with pytest.raises(ModelApiError):
            application.run(investigation.investigation_id)

        saved = store.get_investigation(investigation.investigation_id)
        assert saved.status.value == "FAILED"
        assert saved.stop_reason.value == "UNRECOVERABLE_ERROR"
        assert evidence_store.all_calls() == ()
    finally:
        fixture.cleanup()
