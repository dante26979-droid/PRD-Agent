from __future__ import annotations

import base64
from urllib.parse import quote, urlencode

from prd_agent.integrations.credentials import CredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.integrations.http import JsonHttpTransport, validate_https_base_url
from prd_agent.repository.models import BlobContent, BlobEntry

from .models import RemoteRepositoryBinding


class GitLabRepositoryGateway:
    _MAX_RETRY_AFTER_SECONDS = 300

    def __init__(
        self,
        transport: JsonHttpTransport,
        credentials: CredentialResolver,
        *,
        base_url: str = "https://gitlab.com/api/v4",
        timeout_seconds: float = 10,
        max_tree_pages: int = 100,
    ) -> None:
        self.transport = transport
        self.credentials = credentials
        self.base_url = validate_https_base_url(base_url)
        self.timeout_seconds = timeout_seconds
        self.max_tree_pages = max_tree_pages

    def resolve_commit(self, binding: RemoteRepositoryBinding, revision: str) -> str:
        response, _ = self._get(
            binding,
            (
                f"/projects/{self._project(binding)}/repository/commits/"
                f"{quote(revision, safe='')}"
            ),
        )
        if not isinstance(response, dict) or not isinstance(response.get("id"), str):
            raise self._invalid()
        return response["id"]

    def list_blobs(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
    ) -> tuple[BlobEntry, ...]:
        values: dict[str, BlobEntry] = {}
        page = "1"
        seen_pages: set[str] = set()
        for _ in range(self.max_tree_pages):
            if page in seen_pages:
                raise self._invalid()
            seen_pages.add(page)
            query = urlencode(
                {
                    "ref": commit_sha,
                    "recursive": "true",
                    "per_page": "100",
                    "page": page,
                }
            )
            response, headers = self._get(
                binding,
                f"/projects/{self._project(binding)}/repository/tree?{query}",
            )
            if not isinstance(response, list):
                raise self._invalid()
            for item in response:
                if not isinstance(item, dict) or item.get("type") != "blob":
                    continue
                try:
                    entry = BlobEntry(
                        path=str(item["path"]),
                        blob_id=str(item["id"]),
                        size=int(item.get("size", 0)),
                        mode=str(item["mode"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise self._invalid() from exc
                values[entry.path] = entry
            next_page = headers.get("x-next-page") or headers.get("X-Next-Page")
            if not next_page:
                return tuple(sorted(values.values(), key=lambda item: item.path))
            page = str(next_page)
        raise IntegrationError(
            IntegrationErrorCode.PAYLOAD_TOO_LARGE,
            "repository tree exceeded the page limit",
        )

    def read_blob(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
        path: str,
    ) -> BlobContent:
        query = urlencode({"ref": commit_sha})
        response, _ = self._get(
            binding,
            (
                f"/projects/{self._project(binding)}/repository/files/"
                f"{quote(path, safe='')}?{query}"
            ),
        )
        if (
            not isinstance(response, dict)
            or response.get("encoding") != "base64"
        ):
            raise self._invalid()
        try:
            data = base64.b64decode(str(response["content"]), validate=True)
            blob_id = str(response["blob_id"])
        except (KeyError, ValueError) as exc:
            raise self._invalid() from exc
        return BlobContent(path=path, blob_id=blob_id, data=data)

    def _get(self, binding: RemoteRepositoryBinding, path: str):
        token = self.credentials.resolve(binding.connection_id)
        response = self.transport.request(
            "GET",
            self.base_url + path,
            headers={
                "Accept": "application/json",
                "PRIVATE-TOKEN": token,
            },
            timeout_seconds=self.timeout_seconds,
        )
        self._raise_for_status(response.status_code, response.headers)
        return response.body, response.headers

    @staticmethod
    def _project(binding: RemoteRepositoryBinding) -> str:
        return quote(binding.provider_repository_id, safe="")

    @staticmethod
    def _invalid() -> IntegrationError:
        return IntegrationError(
            IntegrationErrorCode.INVALID_PROVIDER_RESPONSE,
            "provider returned an invalid response",
        )

    @staticmethod
    def _raise_for_status(status: int, headers) -> None:
        if 200 <= status < 300:
            return
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
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        retry_after_seconds = (
            min(int(retry_after), GitLabRepositoryGateway._MAX_RETRY_AFTER_SECONDS)
            if str(retry_after).isdigit()
            else None
        )
        raise IntegrationError(
            code,
            "repository provider request failed",
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
