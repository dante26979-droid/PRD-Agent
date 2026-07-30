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
