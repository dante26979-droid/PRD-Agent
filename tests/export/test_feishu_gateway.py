from prd_agent.export.feishu import FeishuDocumentGateway
from prd_agent.export.formatter import FeishuExportFormatter
from prd_agent.integrations.credentials import StaticCredentialResolver
from prd_agent.integrations.http import HttpResponse


class FakeTransport:
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
        if url.endswith("/documents"):
            return HttpResponse(
                200,
                {},
                {
                    "code": 0,
                    "data": {
                        "document": {
                            "document_id": "docx-1",
                            "revision_id": 1,
                        }
                    },
                },
            )
        return HttpResponse(200, {}, {"code": 0, "data": {}})


def test_feishu_gateway_creates_document_and_structured_blocks_without_leaking_token():
    transport = FakeTransport()
    gateway = FeishuDocumentGateway(
        transport,
        StaticCredentialResolver({"feishu-connection": "secret-token"}),
        connection_id="feishu-connection",
        document_host="example.feishu.cn",
    )
    document = FeishuExportFormatter().format(
        "# 标题\n\n## 目标\n\n正文。\n\n- 条目",
        document_version=1,
    )

    result = gateway.create_document(document, idempotency_key="business-key")

    assert result.external_id == "docx-1"
    assert result.safe_url == "https://example.feishu.cn/docx/docx-1"
    assert len(transport.requests) == 2
    children = transport.requests[1][3]["children"]
    assert [item["block_type"] for item in children] == [3, 4, 2, 12]
    assert all("secret-token" not in url for _, url, _, _ in transport.requests)
    assert all(
        headers["Authorization"] == "Bearer secret-token"
        for _, _, headers, _ in transport.requests
    )
