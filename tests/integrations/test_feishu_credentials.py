from prd_agent.integrations.feishu_credentials import (
    FeishuTenantAccessTokenResolver,
)
from prd_agent.integrations.http import HttpResponse


class TokenTransport:
    def __init__(self) -> None:
        self.requests = []

    def request(
        self,
        method,
        url,
        *,
        headers=None,
        json_body=None,
        timeout_seconds=10,
    ):
        self.requests.append((method, url, headers, json_body))
        return HttpResponse(
            200,
            {},
            {
                "code": 0,
                "msg": "success",
                "tenant_access_token": "tenant-token",
                "expire": 7200,
            },
        )


def test_app_credentials_are_exchanged_once_and_cached_before_expiry():
    transport = TokenTransport()
    resolver = FeishuTenantAccessTokenResolver(
        transport,
        app_id="cli_test",
        app_secret="app-secret",
        now=lambda: 1000.0,
    )

    assert resolver.resolve("feishu-local") == "tenant-token"
    assert resolver.resolve("feishu-local") == "tenant-token"
    assert len(transport.requests) == 1
    method, url, headers, body = transport.requests[0]
    assert method == "POST"
    assert url.endswith("/auth/v3/tenant_access_token/internal")
    assert headers == {"Content-Type": "application/json; charset=utf-8"}
    assert body == {"app_id": "cli_test", "app_secret": "app-secret"}
    assert "app-secret" not in repr(resolver)
