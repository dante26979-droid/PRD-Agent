import pytest

from prd_agent.export.feishu import (
    FeishuDocumentGateway,
    FeishuWikiDocumentGateway,
)
from prd_agent.export.formatter import FeishuExportFormatter
from prd_agent.integrations.credentials import StaticCredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
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


class WikiTransport:
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
        if "/wiki/v2/spaces/get_node?" in url:
            return HttpResponse(
                200,
                {},
                {
                    "code": 0,
                    "data": {
                        "node": {
                            "node_token": "wiki-node-1",
                            "obj_token": "docx-under-wiki",
                            "obj_type": "docx",
                            "title": "目标文档",
                        }
                    },
                },
            )
        if url.endswith("/raw_content"):
            return HttpResponse(
                200,
                {},
                {"code": 0, "data": {"content": "当前飞书正文"}},
            )
        if url.endswith("/documents/docx-under-wiki"):
            return HttpResponse(
                200,
                {},
                {
                    "code": 0,
                    "data": {
                        "document": {
                            "document_id": "docx-under-wiki",
                            "revision_id": 7,
                        }
                    },
                },
            )
        if url.endswith("/children?page_size=500"):
            return HttpResponse(
                200,
                {},
                {"code": 0, "data": {"items": [{"block_id": "old-block"}]}},
            )
        if url.endswith("/children/batch_delete"):
            return HttpResponse(200, {}, {"code": 0, "data": {}})
        if url.endswith("/children"):
            return HttpResponse(200, {}, {"code": 0, "data": {}})
        raise AssertionError((method, url))


def test_wiki_gateway_reads_the_docx_behind_the_configured_node():
    gateway = FeishuWikiDocumentGateway(
        WikiTransport(),
        StaticCredentialResolver({"feishu-local": "tenant-token"}),
        connection_id="feishu-local",
        document_host="example.feishu.cn",
        wiki_node_token="wiki-node-1",
    )

    assert gateway.read_document() == "当前飞书正文"


def test_first_export_replaces_the_configured_wiki_document_in_place():
    transport = WikiTransport()
    gateway = FeishuWikiDocumentGateway(
        transport,
        StaticCredentialResolver({"feishu-local": "tenant-token"}),
        connection_id="feishu-local",
        document_host="example.feishu.cn",
        wiki_node_token="wiki-node-1",
    )
    document = FeishuExportFormatter().format(
        "# 新 PRD\n\n替换后的正文。",
        document_version=1,
    )

    result = gateway.create_document(document, idempotency_key="first-export")

    assert result.external_id == "docx-under-wiki"
    assert result.safe_url == "https://example.feishu.cn/wiki/wiki-node-1"
    assert not any(
        method == "POST" and url.endswith("/docx/v1/documents")
        for method, url, _, _ in transport.requests
    )
    assert any(
        url.endswith("/children/batch_delete")
        for _, url, _, _ in transport.requests
    )
    assert any(
        method == "POST" and url.endswith("/children")
        for method, url, _, _ in transport.requests
    )


def test_feishu_scope_error_is_reported_as_unauthorized_even_on_http_400():
    class ScopeDeniedTransport:
        def request(self, *args, **kwargs):
            return HttpResponse(
                400,
                {},
                {
                    "code": 99991672,
                    "msg": "required application identity scope is missing",
                },
            )

    gateway = FeishuWikiDocumentGateway(
        ScopeDeniedTransport(),
        StaticCredentialResolver({"feishu-local": "tenant-token"}),
        connection_id="feishu-local",
        document_host="example.feishu.cn",
        wiki_node_token="wiki-node-1",
    )

    with pytest.raises(IntegrationError) as captured:
        gateway.read_document()

    assert captured.value.code == IntegrationErrorCode.UNAUTHORIZED


def test_overwrite_preserves_permission_error_when_delete_was_rejected():
    class DeleteDeniedTransport(WikiTransport):
        def request(
            self,
            method,
            url,
            *,
            headers=None,
            json_body=None,
            timeout_seconds=10,
        ):
            if url.endswith("/children/batch_delete"):
                return HttpResponse(
                    403,
                    {},
                    {"code": 1770032, "msg": "forBidden"},
                )
            return super().request(
                method,
                url,
                headers=headers,
                json_body=json_body,
                timeout_seconds=timeout_seconds,
            )

    gateway = FeishuWikiDocumentGateway(
        DeleteDeniedTransport(),
        StaticCredentialResolver({"feishu-local": "tenant-token"}),
        connection_id="feishu-local",
        document_host="example.feishu.cn",
        wiki_node_token="wiki-node-1",
    )
    document = FeishuExportFormatter().format(
        "# 新 PRD\n\n替换后的正文。",
        document_version=1,
    )

    with pytest.raises(IntegrationError) as captured:
        gateway.create_document(document, idempotency_key="denied-export")

    assert captured.value.code == IntegrationErrorCode.UNAUTHORIZED
