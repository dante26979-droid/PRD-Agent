import pytest

from prd_agent.production.config import (
    DeploymentEnvironment,
    cors_origins,
    llm_settings,
)


def test_local_cors_has_safe_development_defaults(monkeypatch):
    monkeypatch.delenv("PRD_AGENT_CORS_ORIGINS", raising=False)

    assert cors_origins(DeploymentEnvironment.LOCAL) == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "*",
        "http://app.example.com",
        "https://localhost:3000",
        "https://user:password@app.example.com",
        "https://app.example.com/path",
    ],
)
def test_production_cors_rejects_missing_or_unsafe_origins(
    monkeypatch,
    value,
):
    monkeypatch.setenv("PRD_AGENT_CORS_ORIGINS", value)

    with pytest.raises(RuntimeError, match="PRD_AGENT_CORS_ORIGINS"):
        cors_origins(DeploymentEnvironment.PRODUCTION)


def test_production_cors_accepts_explicit_https_origins(monkeypatch):
    monkeypatch.setenv(
        "PRD_AGENT_CORS_ORIGINS",
        "https://app.example.com,https://admin.example.com",
    )

    assert cors_origins(DeploymentEnvironment.PRODUCTION) == (
        "https://app.example.com",
        "https://admin.example.com",
    )


def test_production_loads_deepseek_settings_from_secret_file(
    monkeypatch,
    tmp_path,
):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))

    settings = llm_settings(DeploymentEnvironment.PRODUCTION)

    assert settings is not None
    assert settings.provider == "deepseek"
    assert settings.model == "deepseek-v4-pro"
    assert settings.api_key_file == secret
    assert "test-secret" not in repr(settings)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.deepseek.com",
        "https://attacker.example",
        "https://user:password@api.deepseek.com",
        "https://api.deepseek.com/path?token=secret",
    ],
)
def test_production_rejects_unsafe_llm_base_url(
    monkeypatch,
    tmp_path,
    base_url,
):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", base_url)
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))

    with pytest.raises(RuntimeError, match="PRD_AGENT_LLM_BASE_URL"):
        llm_settings(DeploymentEnvironment.PRODUCTION)


def test_llm_settings_apply_bounded_loop_and_transport_limits(
    monkeypatch,
    tmp_path,
):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    monkeypatch.setenv("PRD_AGENT_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("PRD_AGENT_LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("PRD_AGENT_LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("PRD_AGENT_LLM_API_KEY_FILE", str(secret))
    monkeypatch.setenv("PRD_AGENT_LLM_TIMEOUT_SECONDS", "75")
    monkeypatch.setenv("PRD_AGENT_LLM_CONNECT_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_OUTPUT_TOKENS", "2048")
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_ITERATIONS", "7")
    monkeypatch.setenv("PRD_AGENT_LLM_RUN_TOKEN_BUDGET", "18000")
    monkeypatch.setenv("PRD_AGENT_LLM_MAX_TRANSPORT_RETRIES", "1")

    settings = llm_settings(DeploymentEnvironment.PRODUCTION)

    assert settings is not None
    assert settings.timeout_seconds == 75
    assert settings.connect_timeout_seconds == 4
    assert settings.max_output_tokens == 2048
    assert settings.max_iterations == 7
    assert settings.run_token_budget == 18000
    assert settings.max_transport_retries == 1
