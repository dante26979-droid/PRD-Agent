from __future__ import annotations

import hashlib
import uuid

from prd_agent.evidence.deterministic_validator import (
    DeterministicEvidenceValidator,
)
from prd_agent.evidence.models import ExtractionMethod, SourceEvidence
from prd_agent.repository.bindings import normalize_relative_path
from prd_agent.repository.content_policy import RepositoryContentPolicy

from .adapter import ExternalCodingAgentAdapter
from .models import (
    CandidateEvidenceLocator,
    ExternalInvestigationRequest,
    ValidatedExternalInvestigation,
)


class ExternalInvestigationService:
    def __init__(self, reader, adapter: ExternalCodingAgentAdapter) -> None:
        self.reader = reader
        self.adapter = adapter
        self.content_policy = RepositoryContentPolicy()
        self.validator = DeterministicEvidenceValidator(reader)

    def run(
        self,
        request: ExternalInvestigationRequest,
    ) -> ValidatedExternalInvestigation:
        snapshot = self.reader.resolve_snapshot(
            request.repository_id,
            request.resolved_commit_sha,
        )
        provider_result = self.adapter.run(request)
        evidence = []
        rejected = 0
        total_bytes = 0
        candidates = provider_result.candidates[: request.max_candidates]
        rejected += max(len(provider_result.candidates) - len(candidates), 0)
        seen: set[tuple[str, int, int]] = set()
        for candidate in candidates:
            key = (candidate.path, candidate.line_start, candidate.line_end)
            if key in seen:
                rejected += 1
                continue
            seen.add(key)
            try:
                item, blob_size = self._validate_candidate(
                    request,
                    snapshot,
                    candidate,
                    remaining_bytes=request.max_total_bytes - total_bytes,
                )
            except Exception:  # noqa: BLE001 - provider candidates are untrusted
                rejected += 1
                continue
            total_bytes += blob_size
            evidence.append(item)
        status = provider_result.status
        if rejected and status == "COMPLETE":
            status = "PARTIAL"
        if not evidence and provider_result.candidates and status == "COMPLETE":
            status = "PARTIAL"
        return ValidatedExternalInvestigation(
            investigation_id=request.investigation_id,
            status=status,
            evidence=tuple(evidence),
            coverage=provider_result.coverage,
            unknowns=provider_result.unknowns,
            action_trace=provider_result.action_trace,
            rejected_candidates=rejected,
        )

    def _validate_candidate(
        self,
        request: ExternalInvestigationRequest,
        snapshot,
        candidate: CandidateEvidenceLocator,
        *,
        remaining_bytes: int,
    ) -> tuple[SourceEvidence, int]:
        path = normalize_relative_path(candidate.path)
        if request.allowed_path_prefixes and not any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in request.allowed_path_prefixes
        ):
            raise ValueError("candidate path is outside the allowed scope")
        if candidate.line_end < candidate.line_start:
            raise ValueError("candidate line range is invalid")
        blob = self.reader.read_blob(snapshot, path)
        if remaining_bytes <= 0 or len(blob.data) > remaining_bytes:
            raise ValueError("candidate exceeded the byte budget")
        text = self.content_policy.decode_text(
            blob.data,
            max_bytes=remaining_bytes,
        )
        lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        if candidate.line_end > len(lines):
            raise ValueError("candidate line range is outside the source")
        excerpt = "\n".join(
            lines[candidate.line_start - 1 : candidate.line_end]
        )
        excerpt, redacted = self.content_policy.redact(excerpt)
        evidence = SourceEvidence(
            evidence_id=f"evidence-{uuid.uuid4().hex}",
            tool_call_id=f"external-{request.investigation_id}",
            repository_id=request.repository_id,
            resolved_commit_sha=request.resolved_commit_sha,
            path=path,
            line_start=candidate.line_start,
            line_end=candidate.line_end,
            symbol=candidate.symbol,
            excerpt=excerpt,
            content_hash="sha256:"
            + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
            source_blob_id=blob.blob_id,
            extraction_method=ExtractionMethod.EXTERNAL_AGENT_CANDIDATE,
            redaction_applied=redacted,
            access_scope_hash=request.access_scope_hash,
        )
        self.validator.validate(snapshot, evidence)
        return evidence, len(blob.data)
