"""Deterministic quality metrics for the Direct Prompt baseline."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping

from .models import EvalCase
from .agent_trace import AgentLoopTrace


_NEED_RANK = {"NONE": 0, "OPTIONAL": 1, "REQUIRED": 2}


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
            details={"reason": "M0 Step 1/2 configurations have no Evidence or Grounding"},
        ),
    ]


def evaluate_grounding(result) -> list[MetricResult]:
    """Measure operational Grounding outcomes without using hidden ground truth."""

    from prd_agent.grounding.models import ClaimKind, GroundingVerdict

    deterministic = [
        item
        for item in result.claim_assessments
        if item.kind == ClaimKind.CURRENT_STATE
    ]
    unsupported = [
        item
        for item in deterministic
        if item.verdict != GroundingVerdict.SUPPORTED
    ]
    if deterministic:
        unsupported_metric = MetricResult(
            name="unsupported_claim_rate",
            value=_ratio(len(unsupported), len(deterministic)),
            details={
                "unsupported_claim_ids": [item.claim_id for item in unsupported],
                "deterministic_claim_count": len(deterministic),
            },
        )
    else:
        unsupported_metric = MetricResult(
            name="unsupported_claim_rate",
            value=None,
            status="not_applicable",
            details={"reason": "grounding result has no current-state claims"},
        )

    facts = list(result.fact_assessments)
    supported_facts = [
        item for item in facts if item.verdict == GroundingVerdict.SUPPORTED
    ]
    verified_fact_accuracy = MetricResult(
        name="verified_fact_accuracy",
        value=_ratio(len(supported_facts), len(facts)) if facts else None,
        status="measured" if facts else "not_applicable",
        details={
            "mode": "operational_support_rate",
            "supported": len(supported_facts),
            "assessed": len(facts),
        },
    )
    selected_evidence = sum(len(item.evidence_ids) for item in facts)
    supporting_evidence = len(
        {
            reference.evidence_id
            for reference in result.references
        }
    )
    evidence_precision = MetricResult(
        name="evidence_precision",
        value=(
            _ratio(supporting_evidence, selected_evidence)
            if selected_evidence
            else None
        ),
        status="measured" if selected_evidence else "not_applicable",
        details={
            "supporting": supporting_evidence,
            "selected": selected_evidence,
        },
    )
    return [
        unsupported_metric,
        verified_fact_accuracy,
        evidence_precision,
        MetricResult(
            name="grounding_retry_rate",
            value=1.0 if result.retry_count else 0.0,
            details={"retry_count": result.retry_count},
        ),
        MetricResult(
            name="grounding_first_pass_pass_rate",
            value=1.0 if result.confirmable and result.retry_count == 0 else 0.0,
        ),
    ]


def evaluate_agent_operations(
    trace: AgentLoopTrace | Mapping[str, Any],
) -> list[MetricResult]:
    """Measure Agent runtime operations from the versioned, sanitized trace."""

    if not isinstance(trace, AgentLoopTrace):
        trace = AgentLoopTrace.from_dict(trace)
    counters = trace.counters
    results = [
        MetricResult("model_attempt_count", float(counters.model_attempt_count)),
        MetricResult(
            "model_physical_call_count", float(counters.model_physical_call_count)
        ),
        MetricResult("capability_call_count", float(counters.capability_call_count)),
        MetricResult(
            "capability_physical_call_count",
            float(counters.capability_physical_call_count),
        ),
        MetricResult("checkpoint_count", float(counters.checkpoint_count)),
    ]
    if counters.total_tokens is None:
        results.append(
            MetricResult(
                "total_token_count",
                None,
                status="not_applicable",
                details={"reason": "model usage was not reported"},
            )
        )
    else:
        results.append(MetricResult("total_token_count", float(counters.total_tokens)))

    if counters.capability_physical_call_count:
        results.append(
            MetricResult(
                "evidence_yield_per_physical_call",
                round(
                    counters.unique_evidence_count
                    / counters.capability_physical_call_count,
                    4,
                ),
            )
        )
    else:
        results.append(
            MetricResult(
                "evidence_yield_per_physical_call",
                None,
                status="not_applicable",
                details={"reason": "no physical capability call"},
            )
        )

    if counters.coverage_item_count:
        results.append(
            MetricResult(
                "coverage_completion_rate",
                round(
                    counters.coverage_covered_count / counters.coverage_item_count,
                    4,
                ),
            )
        )
    else:
        results.append(
            MetricResult(
                "coverage_completion_rate",
                None,
                status="not_applicable",
                details={"reason": "trace has no coverage domain"},
            )
        )

    covered = [item for item in trace.coverage_transitions if item.after == "COVERED"]
    if covered:
        proxy_count = sum(item.fact_delta is None for item in covered)
        results.append(
            MetricResult(
                "coverage_without_fact_rate",
                round(proxy_count / len(covered), 4),
                details={"measurement_kind": "diagnostic_proxy"},
            )
        )
    else:
        results.append(
            MetricResult(
                "coverage_without_fact_rate",
                None,
                status="not_applicable",
                details={"measurement_kind": "diagnostic_proxy"},
            )
        )

    supported = [item for item in trace.grounding_findings if item.status == "SUPPORTED"]
    if supported:
        references_only = sum(
            item.reason_code == "EVIDENCE_REFERENCE_VALID" for item in supported
        )
        results.append(
            MetricResult(
                "reference_only_support_rate",
                round(references_only / len(supported), 4),
                details={"measurement_kind": "diagnostic_proxy"},
            )
        )
    else:
        results.append(
            MetricResult(
                "reference_only_support_rate",
                None,
                status="not_applicable",
                details={"measurement_kind": "diagnostic_proxy"},
            )
        )

    if counters.replan_count:
        signatures = [item.action_signature for item in trace.capability_calls]
        effective = len(signatures) > 1 and len(set(signatures)) > 1
        results.append(
            MetricResult("replan_effective_change_rate", 1.0 if effective else 0.0)
        )
    else:
        results.append(
            MetricResult(
                "replan_effective_change_rate",
                None,
                status="not_applicable",
                details={"reason": "no replan"},
            )
        )

    physical_signatures = [
        item.action_signature for item in trace.capability_calls if item.physical_call
    ]
    duplicate_count = len(physical_signatures) - len(set(physical_signatures))
    results.append(
        MetricResult("resume_duplicate_physical_call_count", float(duplicate_count))
    )
    return results


def expected_information_need_requiredness(case: EvalCase) -> str:
    values = [item.kind for item in case.expected_information_needs]
    if not values:
        return "NONE"
    return max(values, key=_NEED_RANK.__getitem__)


def evaluate_information_need(
    case: EvalCase, trace: Mapping[str, Any]
) -> list[MetricResult]:
    """Compare one durable Need decision with the case label.

    Only low-cardinality decision fields and call counters are consumed. Prompt,
    question and source content are intentionally excluded.
    """

    raw_need = trace.get("information_need")
    if not isinstance(raw_need, Mapping):
        return [
            MetricResult(
                "information_need_accuracy",
                None,
                status="not_applicable",
                details={"reason": "trace has no durable information need"},
            ),
            MetricResult(
                "none_capability_call_rate",
                None,
                status="not_applicable",
                details={"reason": "trace has no durable information need"},
            ),
            MetricResult(
                "required_investigation_bypass_rate",
                None,
                status="not_applicable",
                details={"reason": "trace has no durable information need"},
            ),
        ]
    actual = str(raw_need.get("requiredness", ""))
    if actual not in _NEED_RANK:
        raise ValueError("trace information need requiredness is invalid")
    expected = expected_information_need_requiredness(case)
    counters = trace.get("counters", {})
    physical_calls = (
        int(counters.get("capability_physical_call_count", 0))
        if isinstance(counters, Mapping)
        else 0
    )
    route = str(raw_need.get("route", ""))
    results = [
        MetricResult(
            "information_need_accuracy",
            1.0 if actual == expected else 0.0,
            details={"expected": expected, "actual": actual},
        )
    ]
    if actual == "NONE":
        results.append(
            MetricResult(
                "none_capability_call_rate",
                1.0 if physical_calls else 0.0,
            )
        )
    else:
        results.append(
            MetricResult(
                "none_capability_call_rate",
                None,
                status="not_applicable",
                details={"reason": "decision is not NONE"},
            )
        )
    if actual == "REQUIRED":
        bypassed = physical_calls == 0 and route != "PAUSE_FOR_HUMAN"
        results.append(
            MetricResult(
                "required_investigation_bypass_rate",
                1.0 if bypassed else 0.0,
            )
        )
    else:
        results.append(
            MetricResult(
                "required_investigation_bypass_rate",
                None,
                status="not_applicable",
                details={"reason": "decision is not REQUIRED"},
            )
        )
    return results


def evaluate_shadow_delta(
    off_trace: AgentLoopTrace | Mapping[str, Any],
    shadow_trace: AgentLoopTrace | Mapping[str, Any],
) -> list[MetricResult]:
    if not isinstance(off_trace, AgentLoopTrace):
        off_trace = AgentLoopTrace.from_dict(off_trace)
    if not isinstance(shadow_trace, AgentLoopTrace):
        shadow_trace = AgentLoopTrace.from_dict(shadow_trace)
    return [
        MetricResult(
            "shadow_extra_model_physical_calls",
            float(
                shadow_trace.counters.model_physical_call_count
                - off_trace.counters.model_physical_call_count
            ),
        ),
        MetricResult(
            "shadow_extra_capability_physical_calls",
            float(
                shadow_trace.counters.capability_physical_call_count
                - off_trace.counters.capability_physical_call_count
            ),
        ),
    ]
