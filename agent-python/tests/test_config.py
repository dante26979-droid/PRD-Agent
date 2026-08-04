from __future__ import annotations

import pytest

from agent.config import AgentSettings


def test_worker_requires_service_token(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.delenv("PRD_AGENT_AGENT_RPC_TOKEN_FILE", raising=False)
    monkeypatch.delenv("PRD_AGENT_AGENT_RPC_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="PRD_AGENT_AGENT_RPC_TOKEN_FILE"):
        AgentSettings.load(require_service_identity=True)


def test_worker_loads_service_token_from_file(monkeypatch, tmp_path):
    token_file = tmp_path / "agent-token"
    token_file.write_text("t" * 32, encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_RPC_TOKEN_FILE", str(token_file))

    settings = AgentSettings.load(require_service_identity=True)

    assert settings.service_token == "t" * 32


def test_llm_settings_load_controlled_loop_budgets(monkeypatch, tmp_path):
    key_file = tmp_path / "llm-key"
    key_file.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-test")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_ITERATIONS", "3")
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_TOOL_CALLS", "2")
    monkeypatch.setenv("PRD_AGENT_LLM_NO_PROGRESS_LIMIT", "1")
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_REPLANS", "0")

    settings = AgentSettings.load()

    assert settings.llm is not None
    assert settings.llm.max_iterations == 3
    assert settings.llm.max_tool_calls == 2
    assert settings.llm.no_progress_limit == 1
    assert settings.llm.max_replans == 0
    assert settings.loop_mode == "langgraph"


def test_loop_mode_rejects_unknown_runtime(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_LOOP_MODE", "unbounded")

    with pytest.raises(RuntimeError, match="langgraph, legacy"):
        AgentSettings.load()


def test_context_policy_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_POLICY_MODE", "shadow")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_POLICY_VERSION", "context-policy.v1-shadow")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_WINDOW_TOKENS", "8000")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_COMPACT_THRESHOLD_TOKENS", "5000")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_TARGET_INPUT_TOKENS", "3000")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "1000")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_EMERGENCY_MARGIN_TOKENS", "500")

    settings = AgentSettings.load()

    assert settings.context_policy.mode == "shadow"
    assert settings.context_policy.version == "context-policy.v1-shadow"
    assert settings.context_policy.hard_input_tokens == 6500


def test_context_compression_rejects_legacy_loop(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_LOOP_MODE", "legacy")
    monkeypatch.setenv("PRD_AGENT_CONTEXT_POLICY_MODE", "enforce")

    with pytest.raises(RuntimeError, match="requires.*langgraph"):
        AgentSettings.load()


def test_project_memory_policy_is_explicit_and_bounded(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_PROJECT_MEMORY_MODE", "shadow")
    monkeypatch.setenv(
        "PRD_AGENT_PROJECT_MEMORY_POLICY_VERSION", "project-memory-policy.v1-shadow"
    )
    monkeypatch.setenv("PRD_AGENT_PROJECT_MEMORY_MAX_RECORDS", "9")
    monkeypatch.setenv("PRD_AGENT_PROJECT_MEMORY_MAX_QUERY_CHARS", "600")

    settings = AgentSettings.load()

    assert settings.project_memory_policy.mode == "shadow"
    assert settings.project_memory_policy.version == "project-memory-policy.v1-shadow"
    assert settings.project_memory_policy.max_records == 9
    assert settings.project_memory_policy.max_query_chars == 600


def test_project_memory_rejects_legacy_loop(monkeypatch):
    monkeypatch.setenv("PRD_AGENT_ENVIRONMENT", "test")
    monkeypatch.setenv("PRD_AGENT_AGENT_LOOP_MODE", "legacy")
    monkeypatch.setenv("PRD_AGENT_PROJECT_MEMORY_MODE", "enforce")

    with pytest.raises(RuntimeError, match="project memory requires.*langgraph"):
        AgentSettings.load()
