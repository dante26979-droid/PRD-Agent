from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from prd_agent.integrations.credentials import CredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.integrations.http import JsonHttpTransport, validate_https_base_url

from .models import (
    CreateIdempotencyCapability,
    ExportBlock,
    ExportBlockType,
    ExportDocument,
    ProviderDocumentResult,
)


_DOCUMENT_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]+\Z")


class FeishuBlockRenderer:
    _TEXT_BLOCK_TYPES = {
        ExportBlockType.PARAGRAPH: 2,
        ExportBlockType.QUOTE: 15,
        ExportBlockType.CODE: 14,
    }
    _HEADING_TYPES = {1: 3, 2: 4, 3: 5}

    def render(self, document: ExportDocument) -> list[dict]:
        values: list[dict] = []
        for block in document.blocks:
            values.extend(self._block(block))
        return values

    def _block(self, block: ExportBlock) -> list[dict]:
        if block.block_type == ExportBlockType.HEADING:
            block_type = self._HEADING_TYPES[block.level or 1]
            return [self._text_block(block_type, block.text or "")]
        if block.block_type in self._TEXT_BLOCK_TYPES:
            return [
                self._text_block(
                    self._TEXT_BLOCK_TYPES[block.block_type],
                    block.text or "",
                )
            ]
        if block.block_type in {
            ExportBlockType.ORDERED_LIST,
            ExportBlockType.UNORDERED_LIST,
        }:
            block_type = (
                13
                if block.block_type == ExportBlockType.ORDERED_LIST
                else 12
            )
            return [self._text_block(block_type, item) for item in block.items]
        if block.block_type == ExportBlockType.TABLE:
            # Basic tables are exported as aligned text rows. This preserves all
            # content without depending on Feishu's nested table-cell mutation API.
            text = "\n".join(" | ".join(row) for row in block.rows)
            return [self._text_block(2, text)]
        if block.block_type == ExportBlockType.DIVIDER:
            return [{"block_type": 22, "divider": {}}]
        return [self._text_block(2, block.text or "")]

    @staticmethod
    def _text_block(block_type: int, text: str) -> dict:
        key = {
            2: "text",
            3: "heading1",
            4: "heading2",
            5: "heading3",
            12: "bullet",
            13: "ordered",
            14: "code",
            15: "quote",
        }[block_type]
        return {
            "block_type": block_type,
            key: {
                "elements": [
                    {
                        "text_run": {
                            "content": text,
                            "text_element_style": {},
                        }
                    }
                ],
                "style": {},
            },
        }


class FeishuDocumentGateway:
    # The create-document API contract does not declare a provider idempotency
    # guarantee. Ambiguous create results therefore require manual review.
    create_idempotency_capability = CreateIdempotencyCapability.NONE

    def __init__(
        self,
        transport: JsonHttpTransport,
        credentials: CredentialResolver,
        *,
        connection_id: str,
        document_host: str,
        folder_token: str | None = None,
        base_url: str = "https://open.feishu.cn/open-apis",
        timeout_seconds: float = 15,
        renderer: FeishuBlockRenderer | None = None,
    ) -> None:
        if not _HOST.fullmatch(document_host):
            raise ValueError("document_host must be a hostname")
        self.transport = transport
        self.credentials = credentials
        self.connection_id = connection_id
        self.document_host = document_host
        self.folder_token = folder_token
        self.base_url = validate_https_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.renderer = renderer or FeishuBlockRenderer()

    def create_document(
        self,
        document: ExportDocument,
        *,
        idempotency_key: str,
    ) -> ProviderDocumentResult:
        payload = {"title": document.title}
        if self.folder_token:
            payload["folder_token"] = self.folder_token
        response = self._request("POST", "/docx/v1/documents", payload)
        try:
            provider_document = response["data"]["document"]
            document_id = str(provider_document["document_id"])
        except (KeyError, TypeError) as exc:
            raise self._invalid() from exc
        self._validate_document_id(document_id)
        try:
            self._append_blocks(document_id, document)
        except IntegrationError as exc:
            raise IntegrationError(
                IntegrationErrorCode.RESULT_UNKNOWN,
                "document was created but its final content could not be confirmed",
                retryable=False,
            ) from exc
        revision = provider_document.get("revision_id")
        return ProviderDocumentResult(
            external_id=document_id,
            safe_url=self._safe_url(document_id),
            title=document.title,
            provider_revision=str(revision) if revision is not None else None,
        )

    def overwrite_document(
        self,
        external_id: str,
        document: ExportDocument,
        *,
        idempotency_key: str,
        expected_revision: str | None,
    ) -> ProviderDocumentResult:
        self._validate_document_id(external_id)
        metadata = self._request(
            "GET",
            f"/docx/v1/documents/{quote(external_id, safe='')}",
        )
        current_revision = self._revision(metadata)
        if (
            expected_revision is not None
            and current_revision is not None
            and expected_revision != current_revision
        ):
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_CHANGED,
                "bound document changed after the last export",
            )
        items = self._children(external_id)
        try:
            if items:
                self._request(
                    "DELETE",
                    (
                        f"/docx/v1/documents/{quote(external_id, safe='')}/"
                        f"blocks/{quote(external_id, safe='')}/children/batch_delete"
                    ),
                    {"start_index": 0, "end_index": len(items)},
                )
            self._append_blocks(external_id, document)
        except IntegrationError as exc:
            raise IntegrationError(
                IntegrationErrorCode.RESULT_UNKNOWN,
                "bound document replacement requires manual review",
                retryable=False,
            ) from exc
        refreshed = self._request(
            "GET",
            f"/docx/v1/documents/{quote(external_id, safe='')}",
        )
        return ProviderDocumentResult(
            external_id=external_id,
            safe_url=self._safe_url(external_id),
            title=document.title,
            provider_revision=self._revision(refreshed),
        )

    def _append_blocks(self, document_id: str, document: ExportDocument) -> None:
        children = self.renderer.render(document)
        if not children:
            return
        self._request(
            "POST",
            (
                f"/docx/v1/documents/{quote(document_id, safe='')}/"
                f"blocks/{quote(document_id, safe='')}/children"
            ),
            {"children": children},
        )

    def _children(self, document_id: str) -> list:
        query = urlencode({"page_size": 500})
        response = self._request(
            "GET",
            (
                f"/docx/v1/documents/{quote(document_id, safe='')}/"
                f"blocks/{quote(document_id, safe='')}/children?{query}"
            ),
        )
        items = response.get("data", {}).get("items", [])
        if not isinstance(items, list):
            raise self._invalid()
        return items

    def _request(self, method: str, path: str, json_body=None) -> dict:
        token = self.credentials.resolve(self.connection_id)
        response = self.transport.request(
            method,
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json_body=json_body,
            timeout_seconds=self.timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            self._raise_for_status(response.status_code)
        if not isinstance(response.body, dict):
            raise self._invalid()
        code = response.body.get("code")
        if code != 0:
            if code in {99991663, 99991668, 99991672}:
                raise IntegrationError(
                    IntegrationErrorCode.UNAUTHORIZED,
                    "Feishu authorization is not available",
                )
            raise IntegrationError(
                IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
                "Feishu rejected the document operation",
            )
        return response.body

    def _safe_url(self, document_id: str) -> str:
        return f"https://{self.document_host}/docx/{document_id}"

    @staticmethod
    def _revision(response: dict) -> str | None:
        document = response.get("data", {}).get("document", {})
        value = document.get("revision_id")
        return str(value) if value is not None else None

    @staticmethod
    def _validate_document_id(document_id: str) -> None:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise IntegrationError(
                IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
                "Feishu returned an invalid document identifier",
            )

    @staticmethod
    def _invalid() -> IntegrationError:
        return IntegrationError(
            IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
            "Feishu returned an invalid response",
        )

    @staticmethod
    def _raise_for_status(status: int) -> None:
        if status in {401, 403}:
            code, retryable = IntegrationErrorCode.UNAUTHORIZED, False
        elif status == 404:
            code, retryable = IntegrationErrorCode.RESOURCE_NOT_FOUND, False
        elif status == 429:
            code, retryable = IntegrationErrorCode.RATE_LIMITED, True
        elif status >= 500:
            code, retryable = IntegrationErrorCode.PROVIDER_UNAVAILABLE, True
        else:
            code, retryable = IntegrationErrorCode.INVALID_PROVIDER_RESPONSE, False
        raise IntegrationError(
            code,
            "Feishu document operation failed",
            retryable=retryable,
        )
