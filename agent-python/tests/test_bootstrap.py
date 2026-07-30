from __future__ import annotations

import json

import httpx
import pytest

from agent.bootstrap import RemoteAgentLoop, build_agent_loop
from agent.checkpoint import CheckpointCodec, CheckpointError
from agent.capability import PrdCatalogHit, PrdSection, RepositorySearchHit
from agent.context import Lease, RunContext


def test_default_test_runtime_turns_agent_run_into_recoverable_draft(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.delenv("PRD_AGENT_LLM_PROVIDER", raising=False)

    loop = build_agent_loop()
    result = loop(
        RunContext(
            run_id="run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="设计一个仓库证据驱动的 PRD",
            workflow_version="agent-runtime.v1",
            checkpoint=b"",
        )
    )

    draft = json.loads(result.draft_patch)
    checkpoint = CheckpointCodec().decode(result.checkpoint)

    assert result.attempt.status == "SUCCEEDED"
    assert result.attempt.provider == "deterministic"
    assert result.checkpoint_sequence == 1
    assert checkpoint.run_id == "run-1"
    assert draft["task_id"] == "task-1"
    assert "仓库证据驱动" in draft["markdown"]


def test_production_runtime_refuses_deterministic_model_fallback(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "production")
    monkeypatch.delenv("PRD_AGENT_LLM_PROVIDER", raising=False)

    with pytest.raises(RuntimeError, match="PRD_AGENT_LLM_PROVIDER is required"):
        build_agent_loop()


def test_remote_runtime_can_roll_back_to_legacy_loop(monkeypatch, tmp_path):
    secret = tmp_path / "llm-key"
    secret.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_LOOP_MODE", "legacy")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-test")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))

    loop = build_agent_loop()

    assert isinstance(loop, RemoteAgentLoop)


def test_production_runtime_uses_remote_structured_model(monkeypatch, tmp_path):
    secret = tmp_path / "llm-key"
    secret.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-test")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))

    planned = []

    def respond(request: httpx.Request):
        assert len(planned) == 1
        assert planned[0].status == "PLANNED"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(
            200,
            headers={"x-request-id": "provider-request-1"},
            json={
                "id": "response-1",
                "model": "deepseek-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "markdown": "# PRD\n\n远程模型生成的 Working Draft",
                                    "evidence": [],
                                },
                                ensure_ascii=False,
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 42},
            },
        )

    loop = build_agent_loop(transport=httpx.MockTransport(respond))
    result = loop(
        RunContext(
            run_id="run-remote",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-remote",
            task_message="生成远程模型 PRD",
            workflow_version="agent-runtime.v1",
            checkpoint=b"",
            plan_model_attempt=planned.append,
        )
    )

    assert result.attempt.provider == "deepseek"
    assert result.attempt.status == "SUCCEEDED"
    assert result.attempt.attempt_key == planned[0].attempt_key
    assert json.loads(result.attempt.token_usage_json)["total_tokens"] == 42
    assert "远程模型生成" in json.loads(result.draft_patch)["markdown"]


def test_runtime_rejects_checkpoint_from_another_agent_run(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.delenv("PRD_AGENT_LLM_PROVIDER", raising=False)
    checkpoint = CheckpointCodec().encode(
        workflow_version="agent-runtime.v1",
        run_id="run-original",
        task_version=1,
        sequence=1,
        payload={"stage": "DRAFTED"},
    )

    with pytest.raises(CheckpointError, match="does not match Agent Run"):
        build_agent_loop()(
            RunContext(
                run_id="run-other",
                tenant_id="tenant-1",
                owner_id="owner-1",
                task_id="task-1",
                task_message="继续生成",
                workflow_version="agent-runtime.v1",
                checkpoint=checkpoint,
                checkpoint_sequence=1,
            )
        )


def test_remote_runtime_grounds_draft_through_capability_gateway(monkeypatch, tmp_path):
    secret = tmp_path / "llm-key"
    secret.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-test")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))
    responses = iter(
        [
            {"repository_queries": ["Go Dispatcher"], "prd_query": ""},
            {"markdown": "# PRD\n\n基于 Dispatcher 证据生成", "evidence": ["README.md:10"]},
        ]
    )

    def respond(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(next(responses), ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 10},
            },
        )

    class FakeCapability:
        def search_repository(self, **kwargs):
            assert kwargs["binding_id"] == "binding-1"
            assert kwargs["revision"] == "a" * 40
            return (
                RepositorySearchHit(
                    path="README.md",
                    line=10,
                    snippet="Dispatcher owns the Agent Run lease",
                ),
            )

        def close(self):
            pass

    loop = build_agent_loop(
        transport=httpx.MockTransport(respond),
        capability_factory=lambda context: FakeCapability(),
    )
    result = loop(
        RunContext(
            run_id="run-grounded",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="生成 Dispatcher 设计 PRD",
            workflow_version="agent-runtime.v1",
            checkpoint=b"",
            dispatch_id="dispatch-1",
            lease=Lease("run-grounded", "lease-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
            repository_binding_id="binding-1",
            repository_revision="a" * 40,
        )
    )

    assert result.attempt.operation == "plan_or_generate_working_draft"
    assert result.additional_attempts[0].operation == "generate_working_draft"
    assert result.evidence[0].locator == (
        "github://binding-1@" + ("a" * 40) + "/README.md#L10"
    )
    assert "基于 Dispatcher" in json.loads(result.draft_patch)["markdown"]


def test_remote_runtime_fetches_historical_prd_source_before_drafting(
    monkeypatch,
    tmp_path,
):
    secret = tmp_path / "llm-key"
    secret.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "production")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-test")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))
    responses = iter(
        [
            {"repository_queries": [], "prd_query": "历史 Agent 边界"},
            {"markdown": "# PRD\n\n参考历史 PRD 约束生成"},
        ]
    )

    def respond(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "model": "deepseek-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(next(responses), ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 8},
            },
        )

    class FakeCapability:
        def search_prd_catalog(self, **kwargs):
            return (
                PrdCatalogHit(
                    locator_id="locator-1",
                    source_revision="revision-1",
                    title="历史 Agent 设计",
                    excerpt="Go owns Control Plane",
                ),
            )

        def fetch_prd_sections(self, locator_ids):
            assert locator_ids == ["locator-1"]
            return (
                PrdSection(
                    locator_id="locator-1",
                    source_revision="revision-1",
                    title="边界",
                    markdown="Python only owns the Agent Loop",
                ),
            )

        def close(self):
            pass

    result = build_agent_loop(
        transport=httpx.MockTransport(respond),
        capability_factory=lambda context: FakeCapability(),
    )(
        RunContext(
            run_id="run-history",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="参考历史 PRD",
            workflow_version="agent-runtime.v1",
            checkpoint=b"",
            dispatch_id="dispatch-1",
            lease=Lease("run-history", "lease-1", "worker-1", 1, "2030-01-01T00:00:00Z"),
        )
    )

    assert result.evidence[0].source_type == "prd"
    assert result.evidence[0].locator == "locator-1"
