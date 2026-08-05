from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Mapping

from agent.context import RunContext
from agent.unit.models import (
    LockedOutline,
    OutlineCandidate,
    RequirementBrief,
    RunPurpose,
    UnitCandidate,
    UnitExecutionRequest,
    UnitRunRequest,
    UnitRunResult,
)
from agent.unit.execution import ReviewableUnitExecutionModule
from agent.unit.runner import ReviewableUnitModule
from agent.unit.transport import decode_unit_scope


StructuredGenerator = Callable[[str, Mapping[str, object]], Mapping[str, object]]
GroundingEvaluator = Callable[[UnitCandidate], tuple[Mapping[str, object], ...]]


@dataclass(frozen=True)
class ReviewableUnitRuntime:
    """Purpose router used by the production graph for one scoped output."""

    def run(
        self,
        context: RunContext,
        generate: StructuredGenerator,
        *,
        grounding_evaluator: GroundingEvaluator,
    ) -> UnitRunResult:
        if context.workflow_version != "agent-runtime.v4" or context.unit_scope is None:
            raise ValueError("reviewable unit runs require v4 and a frozen unit scope")
        scope = decode_unit_scope(context.run_purpose, context.unit_scope)
        base_candidate = self._base_candidate(scope)
        request = UnitRunRequest(
            purpose=scope.purpose,
            scope=scope,
            task_message=context.task_message,
            base_candidate=base_candidate,
        )

        execution_request = self._execution_request(context, scope, base_candidate)
        if execution_request is not None:
            return ReviewableUnitExecutionModule(
                outline_generator=lambda item: self._outline_candidate(
                    item,
                    generate(
                        "plan_outline", self._payload(item, "outline-candidate.v1")
                    ),
                ),
                unit_generator=lambda item: self._unit_candidate(
                    item,
                    generate("generate_unit", self._payload(item, "unit-candidate.v1")),
                ),
                unit_reviser=lambda item: self._unit_patch(
                    item,
                    generate("revise_unit", self._payload(item, "unit-patch.v1")),
                ),
                grounding_evaluator=grounding_evaluator,
            ).run(execution_request)
        module = ReviewableUnitModule(
            outline_generator=lambda item: self._outline_candidate(
                item,
                generate(
                    "plan_outline", self._payload(item, "outline-candidate.v1")
                ),
            ),
            unit_generator=lambda item: self._unit_candidate(
                item,
                generate("generate_unit", self._payload(item, "unit-candidate.v1")),
            ),
            unit_reviser=lambda item: self._unit_patch(
                item,
                generate("revise_unit", self._payload(item, "unit-patch.v1")),
            ),
            grounding_evaluator=grounding_evaluator,
        )
        return module.run(request)

    @staticmethod
    def _execution_request(
        context: RunContext,
        scope,
        base_candidate: UnitCandidate | None,
    ) -> UnitExecutionRequest | None:
        raw = scope.requirement_brief_ref
        if scope.purpose is RunPurpose.PLAN_OUTLINE:
            return UnitExecutionRequest(
                purpose=scope.purpose,
                scope=scope,
                requirement_brief=RequirementBrief(context.task_message),
            )
        if raw.startswith("execution-input-json:"):
            value = json.loads(raw.removeprefix("execution-input-json:"))
            brief_value = value.get("requirement_brief", {})
            outline_value = value.get("locked_outline")
            if not isinstance(brief_value, Mapping) or not isinstance(outline_value, Mapping):
                raise ValueError("execution input requires explicit brief and locked outline")
            outline = OutlineCandidate.from_dict(outline_value)
            brief = RequirementBrief(
                str(brief_value.get("text", "")),
                tuple(str(item) for item in brief_value.get("required_content", ())),
            )
            return UnitExecutionRequest(
                purpose=scope.purpose,
                scope=scope,
                requirement_brief=brief,
                locked_outline=LockedOutline(scope.outline_id, scope.outline_version, outline),
                confirmed_context=scope.confirmed_context,
                base_candidate=base_candidate,
            )
        # Expand-only compatibility for v4 runs created before Phase 8.
        if scope.purpose is RunPurpose.FULL_REVIEW and raw.startswith("outline-json:"):
            outline = OutlineCandidate.from_dict(
                json.loads(raw.removeprefix("outline-json:"))
            )
            return UnitExecutionRequest(
                purpose=scope.purpose,
                scope=scope,
                requirement_brief=RequirementBrief(context.task_message),
                locked_outline=LockedOutline(scope.outline_id, scope.outline_version, outline),
                confirmed_context=scope.confirmed_context,
            )
        return None

    @staticmethod
    def _payload(request: UnitRunRequest, expected_schema: str) -> dict[str, object]:
        return {
            "unit_operation": request.purpose.value,
            "expected_schema": expected_schema,
            "task_message": request.task_message,
            "unit_scope": request.scope.as_dict(),
            "base_candidate": (
                request.base_candidate.as_dict() if request.base_candidate is not None else None
            ),
            "output_contract": ReviewableUnitRuntime._output_contract(expected_schema),
            "instruction": "Return only the requested schema and do not write outside current_unit_key.",
        }

    @staticmethod
    def _output_contract(expected_schema: str) -> Mapping[str, object]:
        if expected_schema == "outline-candidate.v1":
            return {
                "schema_version": "outline-candidate.v1",
                "title": "non-empty string",
                "requirement_size": "SMALL | MEDIUM | LARGE",
                "nodes": [
                    {
                        "node_key": "unique non-empty string",
                        "parent_key": "empty string or another node_key",
                        "ordinal": "non-negative integer",
                        "title": "non-empty string",
                        "questions": ["string"],
                        "required_content": ["string"],
                        "unit_key": "one of units[].unit_key",
                    }
                ],
                "units": [
                    {
                        "unit_key": "unique non-empty string",
                        "title": "non-empty string",
                        "ordinal": "non-negative integer",
                        "node_keys": ["assigned node_key"],
                        "depends_on": ["another unit_key"],
                    }
                ],
            }
        if expected_schema == "unit-candidate.v1":
            return {
                "schema_version": "unit-candidate.v1",
                "unit_key": "exact unit_scope.current_unit_key",
                "title": "exact unit_scope.current_unit_title",
                "ordinal": "exact unit_scope.current_unit_ordinal",
                "node_keys": ["exact unit_scope.section_node_keys values"],
                "markdown": (
                    "non-empty markdown string whose first heading is exact "
                    "unit_scope.current_unit_title"
                ),
                "claims": [],
                "claim_ids": [],
                "unknown_ids": [],
                "used_fact_ids": [],
            }
        if expected_schema == "unit-patch.v1":
            return {
                "schema_version": "unit-patch.v1",
                "unit_key": "exact unit_scope.current_unit_key",
                "base_content_hash": "exact unit_scope.base_unit_hash",
                "replacement_markdown": "non-empty markdown string",
                "claims": [],
                "resolved_issue_ids": [],
                "preserved_unknown_ids": [],
                "used_fact_ids": [],
            }
        return {"schema_version": expected_schema}

    @staticmethod
    def _outline_candidate(
        request: UnitRunRequest, value: Mapping[str, object]
    ) -> Mapping[str, object]:
        value = ReviewableUnitRuntime._schema_payload(
            value, "outline-candidate.v1"
        )
        if value.get("schema_version") == "outline-candidate.v1" and all(
            key in value for key in ("title", "requirement_size", "nodes", "units")
        ):
            return value
        nested = value.get("outline")
        if not isinstance(nested, Mapping):
            return value
        raw_units = nested.get("units")
        if not isinstance(raw_units, list) or not raw_units:
            return value

        normalized_units: list[dict[str, object]] = []
        normalized_nodes: list[dict[str, object]] = []
        used_keys: set[str] = set()
        for index, raw_unit in enumerate(raw_units, start=1):
            if not isinstance(raw_unit, Mapping):
                return value
            base_key = str(
                raw_unit.get("unit_key") or raw_unit.get("key") or f"unit-{index}"
            ).strip()
            unit_key = base_key or f"unit-{index}"
            if unit_key in used_keys:
                unit_key = f"{unit_key}-{index}"
            used_keys.add(unit_key)
            title = str(raw_unit.get("title") or unit_key).strip() or unit_key
            try:
                ordinal = max(int(raw_unit.get("ordinal", index)), 0)
            except (TypeError, ValueError):
                ordinal = index
            raw_dependencies = raw_unit.get(
                "depends_on", raw_unit.get("dependencies", [])
            )
            dependencies = (
                [str(item).strip() for item in raw_dependencies if str(item).strip()]
                if isinstance(raw_dependencies, list)
                else []
            )
            content = raw_unit.get("content")
            required_content = (
                [str(key) for key in content]
                if isinstance(content, Mapping) and content
                else [title]
            )
            normalized_nodes.append(
                {
                    "node_key": unit_key,
                    "parent_key": "",
                    "ordinal": ordinal,
                    "title": title,
                    "questions": [f"{title}需要明确哪些内容？"],
                    "required_content": required_content,
                    "unit_key": unit_key,
                }
            )
            normalized_units.append(
                {
                    "unit_key": unit_key,
                    "title": title,
                    "ordinal": ordinal,
                    "node_keys": [unit_key],
                    "depends_on": dependencies,
                }
            )

        for unit in normalized_units:
            unit_key = str(unit["unit_key"])
            unit["depends_on"] = [
                item
                for item in unit["depends_on"]
                if item in used_keys and item != unit_key
            ]

        unit_count = len(normalized_units)
        requirement_size = str(nested.get("requirement_size", "")).strip().upper()
        if requirement_size not in {"SMALL", "MEDIUM", "LARGE"}:
            requirement_size = (
                "SMALL" if unit_count <= 3 else "MEDIUM" if unit_count <= 8 else "LARGE"
            )
        title = str(
            nested.get("title") or value.get("title") or request.task_message or "PRD"
        ).strip() or "PRD"
        return {
            "schema_version": "outline-candidate.v1",
            "title": title,
            "requirement_size": requirement_size,
            "nodes": normalized_nodes,
            "units": normalized_units,
        }

    @staticmethod
    def _unit_candidate(
        request: UnitRunRequest, value: Mapping[str, object]
    ) -> Mapping[str, object]:
        value = ReviewableUnitRuntime._schema_payload(value, "unit-candidate.v1")
        return {
            "schema_version": "unit-candidate.v1",
            "unit_key": request.scope.current_unit_key,
            "title": request.scope.current_unit_title,
            "ordinal": request.scope.current_unit_ordinal,
            "node_keys": list(request.scope.section_node_keys),
            "markdown": ReviewableUnitRuntime._locked_markdown(
                request.scope.current_unit_title, value.get("markdown", "")
            ),
            "claims": value.get("claims", []),
            "claim_ids": value.get("claim_ids", []),
            "unknown_ids": value.get("unknown_ids", []),
            "used_fact_ids": value.get("used_fact_ids", []),
        }

    @staticmethod
    def _unit_patch(
        request: UnitRunRequest, value: Mapping[str, object]
    ) -> Mapping[str, object]:
        value = ReviewableUnitRuntime._schema_payload(value, "unit-patch.v1")
        return {
            "schema_version": "unit-patch.v1",
            "unit_key": request.scope.current_unit_key,
            "base_content_hash": request.scope.base_unit_hash,
            "replacement_markdown": ReviewableUnitRuntime._locked_markdown(
                request.scope.current_unit_title,
                value.get("replacement_markdown", value.get("markdown", "")),
            ),
            "claims": value.get("claims", []),
            "resolved_issue_ids": value.get("resolved_issue_ids", []),
            "preserved_unknown_ids": value.get("preserved_unknown_ids", []),
            "used_fact_ids": value.get("used_fact_ids", []),
        }

    @staticmethod
    def _schema_payload(
        value: Mapping[str, object], expected_schema: str
    ) -> Mapping[str, object]:
        if value.get("schema_version") == expected_schema:
            return value
        nested = value.get("output_contract")
        if isinstance(nested, Mapping) and nested.get("schema_version") == expected_schema:
            return nested
        return value

    @staticmethod
    def _locked_markdown(title: str, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            return value
        headings = [
            line.lstrip("#").strip()
            for line in value.splitlines()
            if line.startswith("#")
        ]
        if headings and headings[0] == title:
            return value
        return f"# {title}\n\n{value.lstrip()}"

    @staticmethod
    def _base_candidate(scope) -> UnitCandidate | None:
        if scope.purpose is not RunPurpose.REVISE_UNIT:
            return None
        current = next(
            (item for item in scope.confirmed_context if item.unit_key == scope.current_unit_key),
            None,
        )
        if current is None or not current.markdown:
            raise ValueError("revision scope requires the current base unit content")
        return UnitCandidate(
            unit_key=scope.current_unit_key,
            title=scope.current_unit_title,
            ordinal=scope.current_unit_ordinal,
            node_keys=scope.section_node_keys,
            markdown=current.markdown,
        )
