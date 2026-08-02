from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from agent.quality.models import (
    DocumentQualityIssue,
    DocumentQualityReport,
    QualityCode,
    QualityDisposition,
    QualityOutcome,
    QualityScope,
    QualitySeverity,
)
from agent.unit.models import UnitCandidate


@dataclass(frozen=True)
class UnitQualityRequest:
    candidate: UnitCandidate
    required_content: tuple[str, ...] = ()
    grounding_findings: tuple[Mapping[str, object], ...] = ()
    unresolved_conflict_ids: tuple[str, ...] = ()
    unresolved_unknown_ids: tuple[str, ...] = ()


class DocumentQualityPolicy:
    _sensitive_markers = (
        "authorization:",
        "bearer ",
        "api_key",
        "access_token",
        "refresh_token",
        "private_key",
    )
    _required_aliases = {
        "precondition": ("前置条件", "given", "假设"),
        "trigger": ("触发条件", "用户操作", "系统触发", "when", "当"),
        "expected_result": ("预期结果", "系统应", "then"),
    }

    def __init__(self, *, max_unit_bytes: int = 1024 * 1024) -> None:
        if max_unit_bytes < 1:
            raise ValueError("quality size limit must be positive")
        self._max_unit_bytes = max_unit_bytes

    def evaluate_unit(self, request: UnitQualityRequest) -> DocumentQualityReport:
        candidate = request.candidate
        markdown = candidate.markdown.strip()
        lowered = markdown.lower()
        issues: list[DocumentQualityIssue] = []
        if len(markdown.encode("utf-8")) > self._max_unit_bytes:
            issues.append(self._issue(candidate, QualityCode.CONTENT_TOO_LARGE, QualityDisposition.FATAL, "Unit 内容超过大小上限。"))
        if any(marker in lowered for marker in self._sensitive_markers):
            issues.append(self._issue(candidate, QualityCode.SENSITIVE_CONTENT, QualityDisposition.FATAL, "Unit 包含敏感凭据标记。"))
        missing = [item for item in request.required_content if not self._contains_required(lowered, item)]
        if missing:
            code = (
                QualityCode.AMBIGUOUS_ACCEPTANCE_CRITERION
                if any(item in {"precondition", "trigger", "expected_result"} for item in missing)
                else QualityCode.MISSING_REQUIRED_SECTION
            )
            issues.append(
                self._issue(
                    candidate,
                    code,
                    QualityDisposition.REPAIRABLE,
                    "缺少大纲要求内容：" + ", ".join(missing),
                )
            )
        for finding in request.grounding_findings:
            status = str(finding.get("status", ""))
            claim_type = str(finding.get("claim_type", ""))
            if status in {"UNSUPPORTED", "PARTIAL", "CONFLICTING"} and claim_type == "CURRENT_STATE":
                issues.append(
                    self._issue(
                        candidate,
                        QualityCode.UNSUPPORTED_CURRENT_STATE,
                        QualityDisposition.REQUIRES_GROUNDING,
                        "当前状态 Claim 尚未获得 Supported Fact。",
                    )
                )
                break
        if request.unresolved_conflict_ids:
            issues.append(
                self._issue(
                    candidate,
                    QualityCode.UNRESOLVED_SOURCE_CONFLICT,
                    QualityDisposition.NEEDS_HUMAN,
                    "仍存在未解决的来源冲突。",
                )
            )
        if request.unresolved_unknown_ids and not any(
            marker in markdown for marker in ("待确认", "未知", "假设", "风险")
        ):
            issues.append(
                self._issue(
                    candidate,
                    QualityCode.UNMARKED_UNKNOWN,
                    QualityDisposition.REPAIRABLE,
                    "未解决信息必须明确标记为待确认、未知、假设或风险。",
                )
            )
        return DocumentQualityReport(
            scope=QualityScope.UNIT,
            outcome=_outcome(issues),
            issues=tuple(_deduplicate(issues)),
            unit_hashes={candidate.unit_key: candidate.content_hash},
        )

    def _contains_required(self, lowered_markdown: str, required: str) -> bool:
        aliases = self._required_aliases.get(required.lower(), (required.lower(),))
        return any(alias.lower() in lowered_markdown for alias in aliases)

    @staticmethod
    def _issue(
        candidate: UnitCandidate,
        code: QualityCode,
        disposition: QualityDisposition,
        message: str,
    ) -> DocumentQualityIssue:
        severity = QualitySeverity.BLOCKING
        return DocumentQualityIssue(
            code=code,
            severity=severity,
            disposition=disposition,
            message=message,
            affected_unit_keys=(candidate.unit_key,),
        )


def _outcome(issues: list[DocumentQualityIssue]) -> QualityOutcome:
    if not issues:
        return QualityOutcome.PASSED
    if any(item.disposition in {QualityDisposition.FATAL, QualityDisposition.NEEDS_HUMAN} for item in issues):
        return QualityOutcome.NEEDS_HUMAN
    return QualityOutcome.REPAIR_REQUIRED


def _deduplicate(issues: list[DocumentQualityIssue]) -> list[DocumentQualityIssue]:
    result: dict[str, DocumentQualityIssue] = {}
    for issue in issues:
        result.setdefault(issue.issue_id, issue)
    return list(result.values())
