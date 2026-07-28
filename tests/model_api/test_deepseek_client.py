import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from prd_agent.model_api.deepseek import DeepSeekChatClient
from prd_agent.model_api.errors import ModelApiError
from prd_agent.production.config import LlmSettings


def test_client_returns_structured_model_response(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    observed = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["Authorization"]
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"x-request-id": "request-123"},
            json={
                "id": "chat-123",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"status":"ok"}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            },
        )

    settings = LlmSettings(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        api_key_file=secret,
    )
    client = DeepSeekChatClient(
        settings,
        transport=httpx.MockTransport(handler),
    )

    response = client.complete(
        "只返回 JSON 对象。",
        '{"operation":"ping"}',
        timeout_seconds=30,
    )

    assert response.output == '{"status":"ok"}'
    assert response.model_id == "deepseek-v4-pro"
    assert response.token_usage["total_tokens"] == 14
    assert response.finish_reason == "stop"
    assert response.provider_request_id == "request-123"
    assert observed["authorization"] == "Bearer test-secret"
    assert observed["body"]["response_format"] == {"type": "json_object"}
    assert observed["body"]["thinking"] == {"type": "disabled"}
    assert observed["body"]["stream"] is False


def test_client_retries_rate_limit_then_succeeds(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    calls = 0
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "1"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"status":"ok"}'},
                    }
                ],
                "usage": {"total_tokens": 5},
            },
        )

    client = DeepSeekChatClient(
        LlmSettings(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key_file=secret,
            max_transport_retries=2,
        ),
        transport=httpx.MockTransport(handler),
        sleeper=delays.append,
    )

    response = client.complete("JSON only", "{}", timeout_seconds=30)

    assert response.output == '{"status":"ok"}'
    assert calls == 2
    assert delays == [1.0]


def test_client_maps_authentication_failure_without_leaking_key(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text(
        "test-key-must-never-appear-in-errors\n",
        encoding="utf-8",
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key"}},
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

    with pytest.raises(ModelApiError) as captured:
        client.complete("JSON only", "{}", timeout_seconds=30)

    assert captured.value.code == "authentication"
    assert captured.value.retryable is False
    assert calls == 1
    assert "test-key-must-never-appear-in-errors" not in str(
        captured.value
    )
    assert "test-key-must-never-appear-in-errors" not in repr(
        captured.value
    )


def test_client_lists_available_models(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "deepseek-v4-flash",
                        "object": "model",
                        "owned_by": "deepseek",
                    },
                    {
                        "id": "deepseek-v4-pro",
                        "object": "model",
                        "owned_by": "deepseek",
                    },
                ],
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

    assert client.list_models() == (
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    )


def test_client_rejects_malformed_provider_response(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    client = DeepSeekChatClient(
        LlmSettings(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key_file=secret,
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [], "usage": {}},
            )
        ),
    )

    with pytest.raises(ModelApiError) as captured:
        client.complete("JSON only", "{}", timeout_seconds=30)

    assert captured.value.code == "provider_protocol"
    assert captured.value.retryable is False


def test_client_bounds_timeout_retries(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    calls = 0
    delays = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("provider timed out", request=request)

    client = DeepSeekChatClient(
        LlmSettings(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key_file=secret,
            max_transport_retries=2,
        ),
        transport=httpx.MockTransport(handler),
        sleeper=delays.append,
    )

    with pytest.raises(ModelApiError) as captured:
        client.complete("JSON only", "{}", timeout_seconds=30)

    assert captured.value.code == "transport_timeout"
    assert captured.value.retryable is True
    assert calls == 3
    assert delays == [1.0, 3.0]


def test_client_serializes_model_calls_for_small_server_profile(tmp_path):
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")
    first_started = Event()
    release_first = Event()
    second_started = Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        else:
            second_started.set()
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"status":"ok"}'},
                    }
                ],
                "usage": {"total_tokens": 5},
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.complete,
            "JSON only",
            "{}",
            timeout_seconds=30,
        )
        assert first_started.wait(timeout=2)
        second = executor.submit(
            client.complete,
            "JSON only",
            "{}",
            timeout_seconds=30,
        )
        assert not second_started.wait(timeout=0.1)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert second_started.is_set()
