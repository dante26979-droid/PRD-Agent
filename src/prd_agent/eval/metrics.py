"""Deterministic quality metrics for the Direct Prompt baseline."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .models import EvalCase


@dataclass(frozen=True)
class MetricResult:
    name: str
    value: float | None
    status: str = "measured"
    details: dict[str, Any] = field(default_factory=dict)


SECTION_ALIASES: dict[str, tuple[str, ...]] = {
    "requirement_summary": ("需求摘要", "需求概述", "requirement summary"),
    "scope_boundary": ("范围边界", "范围", "scope boundary"),
    "business_rules": ("业务规则", "business rules"),
    "flow": ("流程", "流程说明", "flow"),
    "acceptance_criteria": ("验收标准", "验收条件", "acceptance criteria"),
    "exceptions": ("异常", "异常处理", "exceptions"),
    "open_questions": ("待确认事项", "待确认", "open questions"),
}


def _section_present(output: str, section: str) -> bool:
    aliases = SECTION_ALIASES.get(section, (section,))
    normalized = output.lower()
    return any(alias.lower() in normalized for alias in aliases)


def _ratio(found: int, total: int) -> float:
    return 1.0 if total == 0 else round(found / total, 4)


def _metric_sections(case: EvalCase, output: str) -> MetricResult:
    found = [section for section in case.required_prd_sections if _section_present(output, section)]
    return MetricResult(
        name="required_section_coverage",
        value=_ratio(len(found), len(case.required_prd_sections)),
        details={"found": found, "required": list(case.required_prd_sections)},
    )


def _metric_keywords(case: EvalCase, output: str) -> MetricResult:
    if not case.expected_keywords:
        return MetricResult(
            name="requirement_coverage",
            value=None,
            status="not_applicable",
            details={"reason": "case has no expected_keywords"},
        )
    found = [keyword for keyword in case.expected_keywords if keyword.lower() in output.lower()]
    return MetricResult(
        name="requirement_coverage",
        value=_ratio(len(found), len(case.expected_keywords)),
        details={"found": found, "required": list(case.expected_keywords)},
    )


def _metric_boundary(case: EvalCase, output: str) -> MetricResult:
    if not case.expected_unknowns:
        return MetricResult(name="boundary_recall", value=1.0, details={"unknowns": []})
    marker_present = any(marker in output for marker in ("范围外", "不包含", "待确认", "未知"))
    value = 1.0 if marker_present else 0.0
    return MetricResult(
        name="boundary_recall",
        value=value,
        details={"marker_present": marker_present},
    )


def _metric_unknowns(case: EvalCase, output: str) -> MetricResult:
    if not case.expected_unknowns:
        return MetricResult(name="unknown_preservation", value=1.0, details={"unknowns": []})
    found = [unknown for unknown in case.expected_unknowns if unknown.lower() in output.lower()]
    return MetricResult(
        name="unknown_preservation",
        value=_ratio(len(found), len(case.expected_unknowns)),
        details={"found": found, "required": list(case.expected_unknowns)},
    )


def _metric_acceptance(case: EvalCase, output: str) -> MetricResult:
    if "acceptance_criteria" not in case.required_prd_sections:
        return MetricResult(
            name="acceptance_criteria_executability",
            value=None,
            status="not_applicable",
            details={"reason": "case does not require acceptance criteria"},
        )
    section = _section_present(output, "acceptance_criteria")
    # A first deterministic rubric: an executable criterion has a condition and an expected action/result.
    condition = bool(re.search(r"当|如果|输入|请求", output))
    expected_result = bool(re.search(r"应|拒绝|返回|成功|失败|提示", output))
    value = 1.0 if section and condition and expected_result else 0.0
    return MetricResult(
        name="acceptance_criteria_executability",
        value=value,
        details={"section": section, "condition": condition, "expected_result": expected_result},
    )


def evaluate_case(case: EvalCase, output: str) -> list[MetricResult]:
    """Evaluate one generated PRD without changing the source run or Ground Truth."""

    if not isinstance(output, str):
        raise TypeError("output must be a string")
    return [
        _metric_sections(case, output),
        _metric_keywords(case, output),
        _metric_boundary(case, output),
        _metric_unknowns(case, output),
        _metric_acceptance(case, output),
        MetricResult(
            name="unsupported_claim_rate",
            value=None,
            status="not_applicable",
            details={"reason": "Direct Prompt baseline has no Evidence or Grounding"},
        ),
    ]
