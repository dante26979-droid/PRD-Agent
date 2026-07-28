from __future__ import annotations

import base64
from urllib.parse import quote

from prd_agent.integrations.credentials import CredentialResolver
from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode
from prd_agent.integrations.http import JsonHttpTransport, validate_https_base_url
from prd_agent.repository.models import BlobContent, BlobEntry

from .models import RemoteRepositoryBinding


class GitHubRepositoryGateway:
    _MAX_RETRY_AFTER_SECONDS = 300

    def __init__(
        self,
        transport: JsonHttpTransport,
        credentials: CredentialResolver,
        *,
        base_url: str = "https://api.github.com",
        api_version: str = "2026-03-10",
        timeout_seconds: float = 10,
    ) -> None:
        self.transport = transport
        self.credentials = credentials
        self.base_url = validate_https_base_url(base_url)
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds

    def resolve_commit(self, binding: RemoteRepositoryBinding, revision: str) -> str:
        response = self._get(
            binding,
            f"/repos/{self._repository(binding)}/commits/{quote(revision, safe='')}",
        )
        return self._required_string(response, "sha")

    def list_blobs(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
    ) -> tuple[BlobEntry, ...]:
        response = self._get(
            binding,
            f"/repos/{self._repository(binding)}/git/trees/{commit_sha}?recursive=1",
        )
        if response.get("truncated") is True:
            raise IntegrationError(
                IntegrationErrorCode.PAYLOAD_TOO_LARGE,
                "repository tree exceeded the provider limit",
            )
        tree = response.get("tree")
        if not isinstance(tree, list):
            raise self._invalid()
        values: list[BlobEntry] = []
        for item in tree:
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            try:
                values.append(
                    BlobEntry(
                        path=str(item["path"]),
                        blob_id=str(item["sha"]),
                        size=int(item.get("size", 0)),
                        mode=str(item["mode"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise self._invalid() from exc
        return tuple(sorted(values, key=lambda item: item.path))

    def read_blob(
        self,
        binding: RemoteRepositoryBinding,
        commit_sha: str,
        path: str,
    ) -> BlobContent:
        response = self._get(
            binding,
            (
                f"/repos/{self._repository(binding)}/contents/"
                f"{quote(path, safe='/')}?ref={commit_sha}"
            ),
        )
        if response.get("type") != "file" or response.get("encoding") != "base64":
            raise self._invalid()
        try:
            data = base64.b64decode(str(response["content"]), validate=True)
            blob_id = str(response["sha"])
        except (KeyError, ValueError) as exc:
            raise self._invalid() from exc
        return BlobContent(path=path, blob_id=blob_id, data=data)

    def _get(self, binding: RemoteRepositoryBinding, path: str) -> dict:
        token = self.credentials.resolve(binding.connection_id)
        response = self.transport.request(
            "GET",
            self.base_url + path,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": self.api_version,
            },
            timeout_seconds=self.timeout_seconds,
        )
        self._raise_for_status(response.status_code, response.headers)
        if not isinstance(response.body, dict):
            raise self._invalid()
        return response.body

    @staticmethod
    def _repository(binding: RemoteRepositoryBinding) -> str:
        parts = binding.provider_repository_id.split("/")
        if len(parts) != 2 or not all(parts):
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_NOT_ALLOWED,
                "repository binding is invalid",
            )
        return "/".join(quote(part, safe="") for part in parts)

    @staticmethod
    def _required_string(value: dict, key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise GitHubRepositoryGateway._invalid()
        return item

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
            min(int(retry_after), GitHubRepositoryGateway._MAX_RETRY_AFTER_SECONDS)
            if str(retry_after).isdigit()
            else None
        )
        raise IntegrationError(
            code,
            "repository provider request failed",
            retryable=retryable,
            retry_after_seconds=retry_after_seconds,
        )
