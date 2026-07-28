from __future__ import annotations

import hashlib

from prd_agent.repository.content_policy import RepositoryContentPolicy
from prd_agent.repository.path_policy import RepositoryPathPolicy
from prd_agent.repository.errors import BlockedPath

from .models import ExtractionMethod, SourceEvidence


class EvidenceValidationError(Exception):
    pass


class DeterministicEvidenceValidator:
    def __init__(self, reader) -> None:
        self.reader = reader
        self.content_policy = RepositoryContentPolicy()
        self.path_policy = RepositoryPathPolicy()

    def validate(self, snapshot, evidence: SourceEvidence) -> None:
        if evidence.repository_id != snapshot.repository_id:
            raise EvidenceValidationError("evidence repository does not match snapshot")
        if evidence.resolved_commit_sha != snapshot.resolved_commit_sha:
            raise EvidenceValidationError("evidence commit does not match snapshot")
        try:
            self.path_policy.validate(evidence.path)
            blob = self.reader.read_blob(snapshot, evidence.path)
        except BlockedPath as exc:
            raise EvidenceValidationError("evidence path is blocked by repository policy") from exc
        if evidence.source_blob_id and evidence.source_blob_id != blob.blob_id:
            raise EvidenceValidationError("evidence blob id does not match snapshot")
        own_hash = "sha256:" + hashlib.sha256(
            evidence.excerpt.encode("utf-8")
        ).hexdigest()
        if own_hash != evidence.content_hash:
            raise EvidenceValidationError("evidence content hash is invalid")
        if evidence.line_start is None and evidence.line_end is None:
            return
        if evidence.line_start is None or evidence.line_end is None:
            raise EvidenceValidationError("evidence line range is incomplete")
        text = self.content_policy.decode_text(blob.data, max_bytes=max(len(blob.data), 1))
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if evidence.line_start < 1 or evidence.line_end > len(lines):
            raise EvidenceValidationError("evidence line range is outside source")
        source_excerpt = "\n".join(
            lines[evidence.line_start - 1 : evidence.line_end]
        )
        if evidence.extraction_method in {
            ExtractionMethod.OPENAPI_PARSE,
            ExtractionMethod.DATABASE_SCHEMA_PARSE,
            ExtractionMethod.RELATED_TEST_SEARCH,
        }:
            source_excerpt = source_excerpt.strip().rstrip(",")
        if evidence.redaction_applied:
            source_excerpt, _ = self.content_policy.redact(source_excerpt)
        if source_excerpt != evidence.excerpt:
            raise EvidenceValidationError("evidence excerpt does not match source locator")
