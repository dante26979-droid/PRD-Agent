from __future__ import annotations

import json

import httpx

from agent.config import LlmSettings
from agent.model import DeepSeekChatClient


def test_deepseek_v4_usage_details_are_not_written_to_durable_counters(
    tmp_path,
) -> None:
    secret = tmp_path / "deepseek_api_key"
    secret.write_text("test-secret\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "disabled"}
        return httpx.Response(
            200,
            json={
                "id": "chat-v4",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"reply":"ok"}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 35,
                    "completion_tokens": 6,
                    "total_tokens": 41,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    client = DeepSeekChatClient(
        LlmSettings(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key_file=secret,
            timeout_seconds=30,
            connect_timeout_seconds=5,
            max_output_tokens=128,
            max_iterations=2,
            max_tool_calls=2,
            no_progress_limit=1,
            max_replans=1,
            run_token_budget=1000,
            max_transport_retries=0,
        ),
        transport=httpx.MockTransport(handler),
    )

    response = client.complete("JSON only", "{}")

    assert response.output == '{"reply":"ok"}'
    assert response.token_usage == {
        "prompt_tokens": 35,
        "completion_tokens": 6,
        "total_tokens": 41,
    }
