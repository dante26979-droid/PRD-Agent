from __future__ import annotations

import hashlib
import re

from prd_agent.domain.enums import SectionStatus

from .models import (
    QualityIssue,
    QualityIssueType,
    QualityResult,
    QualityScope,
    QualitySeverity,
)


def _issue_id(run_id: str, issue_type: QualityIssueType, key: str) -> str:
    digest = hashlib.sha256(
        f"{run_id}:{issue_type.value}:{key}".encode("utf-8")
    ).hexdigest()[:20]
    return f"quality-issue-{digest}"


class DocumentQualityService:
    """Deterministic quality checks; never modifies document content."""

    def check_document(
        self,
        *,
        quality_run_id: str,
        task_id: str,
        document_id: str,
        outline,
        sections,
    ) -> QualityResult:
        issues: list[QualityIssue] = []
        current_sections = tuple(
            item for item in sections if item.status == SectionStatus.CONFIRMED
        )
        covered = {item.node_id for item in current_sections if item.node_id}
        missing = tuple(
            item.node_id for item in outline.nodes if item.node_id not in covered
        )
        if missing:
            issues.append(
                QualityIssue(
                    issue_id=_issue_id(
                        quality_run_id,
                        QualityIssueType.OUTLINE_COVERAGE,
                        ",".join(missing),
                    ),
                    issue_type=QualityIssueType.OUTLINE_COVERAGE,
                    severity=QualitySeverity.BLOCKER,
                    affected_node_ids=missing,
                    description="已确认大纲存在未完成章节",
                    suggested_resolution="补全受影响确认单元并重新确认",
                )
            )
        for section in current_sections:
            if not section.content.strip():
                issues.append(
                    QualityIssue(
                        issue_id=_issue_id(
                            quality_run_id,
                            QualityIssueType.EMPTY_OR_DUPLICATE_CONTENT,
                            section.section_id,
                        ),
                        issue_type=QualityIssueType.EMPTY_OR_DUPLICATE_CONTENT,
                        severity=QualitySeverity.ERROR,
                        affected_node_ids=(section.node_id,)
                        if section.node_id
                        else (),
                        affected_unit_ids=(section.unit_id,),
                        affected_section_ids=(section.section_id,),
                        description="章节内容为空",
                        suggested_resolution="重新生成并确认该章节",
                    )
                )
            if "验收" in section.title and not (
                re.search(r"当|如果|输入|请求", section.content)
                and re.search(r"应|返回|拒绝|成功|失败|提示", section.content)
            ):
                issues.append(
                    QualityIssue(
                        issue_id=_issue_id(
                            quality_run_id,
                            QualityIssueType.ACCEPTANCE_NOT_EXECUTABLE,
                            section.section_id,
                        ),
                        issue_type=QualityIssueType.ACCEPTANCE_NOT_EXECUTABLE,
                        severity=QualitySeverity.ERROR,
                        affected_node_ids=(section.node_id,)
                        if section.node_id
                        else (),
                        affected_unit_ids=(section.unit_id,),
                        affected_section_ids=(section.section_id,),
                        description="验收标准缺少可执行条件或预期结果",
                        suggested_resolution="补充触发条件、动作和可验证结果",
                    )
                )
        blockers = {
            QualitySeverity.BLOCKER,
            QualitySeverity.ERROR,
        }
        return QualityResult(
            quality_run_id=quality_run_id,
            task_id=task_id,
            scope=QualityScope.DOCUMENT,
            scope_id=document_id,
            confirmable=not any(item.severity in blockers for item in issues),
            issues=tuple(issues),
        )
