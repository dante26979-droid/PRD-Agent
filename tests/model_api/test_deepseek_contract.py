import json

import httpx

from prd_agent.domain.commands import StartTask
from prd_agent.domain.enums import TaskStatus
from prd_agent.model_api.deepseek import DeepSeekChatClient
from prd_agent.model_api.models import ModelResponse
from prd_agent.investigation.models import ProposedAction
from prd_agent.production.config import LlmSettings
from prd_agent.storage.memory import InMemoryWorkflowRepository
from prd_agent.workflow.model import JsonWorkflowModelAdapter
from prd_agent.workflow.service import WorkflowService

from tests.workflow.support import first_outline, sufficient_brief


def test_deepseek_client_drives_start_task_through_workflow_contract(
    tmp_path,
):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    outputs = iter([sufficient_brief(), first_outline()])
    operations = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        user_payload = json.loads(request_body["messages"][1]["content"])
        operations.append(user_payload)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                next(outputs),
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                },
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
    workflow = WorkflowService(
        InMemoryWorkflowRepository(),
        JsonWorkflowModelAdapter(client),
    )

    snapshot = workflow.start_task(
        StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="deepseek-start-task",
        )
    )

    assert snapshot.task.status == TaskStatus.OUTLINE_REVIEW
    assert snapshot.outlines[-1].title == "订单创建时间筛选"
    assert operations == [
        {"user_message": "订单列表增加创建时间筛选"},
        {"requirement_brief": sufficient_brief()},
    ]


def test_workflow_repairs_invalid_deepseek_json_once(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    outputs = iter(
        [
            "not-json",
            json.dumps(sufficient_brief(), ensure_ascii=False),
            json.dumps(first_outline(), ensure_ascii=False),
        ]
    )
    system_prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_body = json.loads(request.content)
        system_prompts.append(request_body["messages"][0]["content"])
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": next(outputs)},
                    }
                ],
                "usage": {"total_tokens": 10},
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
    model = JsonWorkflowModelAdapter(client)

    snapshot = WorkflowService(
        InMemoryWorkflowRepository(),
        model,
    ).start_task(
        StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="deepseek-repair",
        )
    )

    assert snapshot.task.status == TaskStatus.OUTLINE_REVIEW
    assert len(system_prompts) == 3
    assert "上次输出未通过 Schema 校验" not in system_prompts[0]
    assert "上次输出未通过 Schema 校验" in system_prompts[1]


def test_workflow_repairs_json_truncated_by_token_limit(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    responses = iter(
        [
            ("length", sufficient_brief()),
            ("stop", sufficient_brief()),
            ("stop", first_outline()),
        ]
    )
    calls = 0
    system_prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        system_prompts.append(
            json.loads(request.content)["messages"][0]["content"]
        )
        finish_reason, output = next(responses)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {
                            "content": json.dumps(
                                output,
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
                "usage": {"total_tokens": 10},
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

    snapshot = WorkflowService(
        InMemoryWorkflowRepository(),
        JsonWorkflowModelAdapter(client),
    ).start_task(
        StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="deepseek-length-repair",
        )
    )

    assert snapshot.task.status == TaskStatus.OUTLINE_REVIEW
    assert calls == 3
    assert "操作：extract_requirement_brief" in system_prompts[1]
    assert "上次输出未通过 Schema 校验" in system_prompts[1]


def test_workflow_repairs_empty_deepseek_content_once(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    outputs = iter(
        [
            "",
            json.dumps(sufficient_brief(), ensure_ascii=False),
            json.dumps(first_outline(), ensure_ascii=False),
        ]
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": next(outputs)},
                    }
                ],
                "usage": {"total_tokens": 10},
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

    snapshot = WorkflowService(
        InMemoryWorkflowRepository(),
        JsonWorkflowModelAdapter(client),
    ).start_task(
        StartTask(
            message="订单列表增加创建时间筛选",
            idempotency_key="deepseek-empty-repair",
        )
    )

    assert snapshot.task.status == TaskStatus.OUTLINE_REVIEW
    assert calls == 3


def test_workflow_adapter_serializes_nested_pydantic_evidence():
    class CaptureModel:
        def complete(
            self,
            system_prompt,
            user_prompt,
            *,
            timeout_seconds,
        ):
            self.payload = json.loads(user_prompt)
            return ModelResponse(
                output='{"content":"ok"}',
                model_id="capture",
            )

    client = CaptureModel()
    action = ProposedAction(
        tool_id="repo_tree",
        arguments={"prefix": "", "max_depth": 2},
        purpose="定位结构",
        target_coverage=("repository_structure",),
    )

    JsonWorkflowModelAdapter(client).complete(
        "generate_confirmation_unit",
        {"investigation_context": {"proposed_action": action}},
    )

    assert client.payload["investigation_context"]["proposed_action"] == (
        action.model_dump(mode="json")
    )
