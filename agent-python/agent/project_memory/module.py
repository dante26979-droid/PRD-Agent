from __future__ import annotations

import copy
import re
from typing import Mapping

from agent.context import RunContext
from agent.runtime import BudgetVector, LedgerCallSpec, RunExecutionLedger
from agent.runtime.idempotency import request_hash

from .models import MemoryBundle, PreparedProjectMemory
from .policy import ProjectMemoryPolicy


_MEMORY_TYPES_BY_OPERATION = {
    "plan_information_need": ("DOMAIN_TERM", "PROJECT_DECISION", "PROJECT_CONSTRAINT", "SOURCE_POINTER", "OPEN_QUESTION", "RISK_NOTE"),
    "plan_outline": ("DOMAIN_TERM", "PROJECT_DECISION", "PROJECT_CONSTRAINT", "WORKFLOW_PREFERENCE", "ACCEPTANCE_PATTERN"),
    "generate_unit": ("DOMAIN_TERM", "PROJECT_DECISION", "PROJECT_CONSTRAINT", "ACCEPTANCE_PATTERN", "SOURCE_POINTER", "RISK_NOTE"),
    "revise_unit": ("PROJECT_DECISION", "PROJECT_CONSTRAINT", "WORKFLOW_PREFERENCE", "ACCEPTANCE_PATTERN", "OPEN_QUESTION"),
    "select_investigation_action": ("SOURCE_POINTER", "OPEN_QUESTION"),
    "plan_or_generate_working_draft": ("DOMAIN_TERM", "PROJECT_DECISION", "PROJECT_CONSTRAINT", "ACCEPTANCE_PATTERN", "SOURCE_POINTER", "OPEN_QUESTION", "RISK_NOTE"),
    "generate_working_draft": ("DOMAIN_TERM", "PROJECT_DECISION", "PROJECT_CONSTRAINT", "ACCEPTANCE_PATTERN", "SOURCE_POINTER", "OPEN_QUESTION", "RISK_NOTE"),
    "repair_working_draft": ("PROJECT_DECISION", "PROJECT_CONSTRAINT", "WORKFLOW_PREFERENCE", "ACCEPTANCE_PATTERN", "OPEN_QUESTION"),
}


class ProjectMemoryModule:
    """Watermarked, permission-scoped project-memory recall for model calls."""

    def __init__(self, policy: ProjectMemoryPolicy = ProjectMemoryPolicy()) -> None:
        self.policy = policy

    def prepare(
        self,
        *,
        context: RunContext,
        gateway: object | None,
        ledger: RunExecutionLedger,
        operation: str,
        operation_sequence: int,
        payload: Mapping[str, object],
    ) -> PreparedProjectMemory:
        original = copy.deepcopy(dict(payload))
        if (
            self.policy.mode == "off"
            or not context.memory_space_id
            or operation not in _MEMORY_TYPES_BY_OPERATION
        ):
            return PreparedProjectMemory(payload=original)
        if not context.memory_assignment_hash or not context.memory_policy_version:
            raise ValueError("PROJECT_MEMORY_ASSIGNMENT_INCOMPLETE")
        if gateway is None or not callable(getattr(gateway, "search_project_memory", None)):
            if self.policy.mode == "shadow":
                return PreparedProjectMemory(payload=original)
            raise ValueError("PROJECT_MEMORY_GATEWAY_REQUIRED")
        query = self._query(context, operation, original)
        memory_types = _MEMORY_TYPES_BY_OPERATION[operation]
        search_spec = LedgerCallSpec(
            entry_kind="CAPABILITY",
            operation="search_project_memory",
            operation_key=(
                f"capability:project_memory:{_safe_segment(operation)}:"
                f"sequence{operation_sequence}"
            ),
            request_hash=request_hash(
                {
                    "memory_assignment_hash": context.memory_assignment_hash,
                    "memory_space_id": context.memory_space_id,
                    "memory_watermark": context.memory_watermark,
                    "memory_policy_version": context.memory_policy_version,
                    "query": query,
                    "operation": operation,
                    "memory_types": memory_types,
                    "limit": self.policy.max_records,
                }
            ),
            reservation=BudgetVector(tool_calls=1),
            outcome_schema="project-memory-search.v1",
        )
        search_outcome = ledger.execute_capability(
            search_spec,
            lambda: gateway.search_project_memory(
                query=query,
                operation=operation,
                memory_types=memory_types,
                limit=self.policy.max_records,
            ),
            _normalize_search_result,
            consumption=lambda _value: BudgetVector(tool_calls=1),
        )
        search = search_outcome.value
        if search["space_id"] != context.memory_space_id or search["memory_watermark"] != context.memory_watermark:
            raise ValueError("PROJECT_MEMORY_WATERMARK_MISMATCH")
        bundle_seed = {
            "run_id": context.run_id,
            "operation": operation,
            "operation_sequence": operation_sequence,
            "memory_assignment_hash": context.memory_assignment_hash,
            "query": query,
            "search_artifact_hash": search_outcome.artifact.content_hash,
            "source_set_hash": search["source_set_hash"],
        }
        bundle_hash = request_hash(bundle_seed)
        bundle = MemoryBundle(
            schema_version="memory-bundle.v1",
            bundle_id="memory-bundle-" + bundle_hash.removeprefix("sha256:")[:24],
            run_id=context.run_id,
            operation=operation,
            operation_sequence=operation_sequence,
            memory_space_id=context.memory_space_id,
            memory_watermark=context.memory_watermark,
            memory_policy_version=context.memory_policy_version,
            memory_assignment_hash=context.memory_assignment_hash,
            query=query,
            records=tuple(search["records"]),
            conflicts=tuple(search["conflicts"]),
            excluded_count=search["excluded_count"],
            source_set_hash=search["source_set_hash"],
            bundle_hash=bundle_hash,
        )
        bundle_spec = LedgerCallSpec(
            entry_kind="LOCAL_DERIVATION",
            operation="build_memory_bundle",
            operation_key=(
                f"memory:build:{_safe_segment(operation)}:sequence{operation_sequence}"
            ),
            request_hash=request_hash(
                {
                    "policy_version": self.policy.version,
                    "bundle_hash": bundle_hash,
                    "search_artifact_hash": search_outcome.artifact.content_hash,
                }
            ),
            reservation=BudgetVector(),
            outcome_schema="memory-bundle.v1",
        )
        bundle_outcome = ledger.execute_derivation(
            bundle_spec,
            bundle.as_dict,
            _validate_bundle,
            artifact_type="PROJECT_MEMORY_BUNDLE",
        )
        effective = original
        if self.policy.mode == "enforce":
            effective = {**original, "project_memory": bundle_outcome.value}
        return PreparedProjectMemory(
            payload=effective,
            bundle=bundle,
            capability_artifact_key=search_outcome.artifact.artifact_key,
            capability_artifact_hash=search_outcome.artifact.content_hash,
            bundle_artifact_key=bundle_outcome.artifact.artifact_key,
            bundle_artifact_hash=bundle_outcome.artifact.content_hash,
            replayed=search_outcome.replayed and bundle_outcome.replayed,
        )

    def _query(
        self,
        context: RunContext,
        operation: str,
        payload: Mapping[str, object],
    ) -> str:
        values = [context.task_message, operation.replace("_", " ")]
        for key in (
            "task_message",
            "requirement",
            "query",
            "unit_scope",
            "quality_issues",
            "user_feedback",
        ):
            if key in payload:
                values.extend(_bounded_strings(payload[key]))
        output = "\n".join(dict.fromkeys(item.strip() for item in values if item.strip()))
        return output[: self.policy.max_query_chars]


def _normalize_search_result(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        output = copy.deepcopy(dict(value))
        if (
            not output.get("space_id")
            or not isinstance(output.get("memory_watermark"), int)
            or output["memory_watermark"] < 0
            or not isinstance(output.get("records"), list)
            or not isinstance(output.get("conflicts"), list)
            or not output.get("source_set_hash")
        ):
            raise ValueError("PROJECT_MEMORY_RESULT_INVALID")
        for record in output["records"]:
            if (
                not isinstance(record, Mapping)
                or not record.get("memory_id")
                or not isinstance(record.get("version"), int)
                or record["version"] < 1
                or not record.get("content_hash")
            ):
                raise ValueError("PROJECT_MEMORY_RESULT_INVALID")
        for conflict in output["conflicts"]:
            if (
                not isinstance(conflict, Mapping)
                or not conflict.get("conflict_id")
                or not isinstance(conflict.get("memory_ids"), list)
                or len(conflict["memory_ids"]) < 2
            ):
                raise ValueError("PROJECT_MEMORY_CONFLICT_INVALID")
        return output
    records = []
    for item in tuple(getattr(value, "records", ())):
        source_refs = []
        for ref in tuple(getattr(item, "source_refs", ())):
            source_refs.append(
                {
                    "source_kind": str(getattr(ref, "source_kind", "")),
                    "binding_id": str(getattr(ref, "binding_id", "")),
                    "source_id": str(getattr(ref, "source_id", "")),
                    "source_version": str(getattr(ref, "source_version", "")),
                    "locator": str(getattr(ref, "locator", "")),
                    "content_hash": str(getattr(ref, "content_hash", "")),
                }
            )
        record = {
            "memory_id": str(getattr(item, "memory_id", "")),
            "version": int(getattr(item, "version", 0)),
            "memory_type": str(getattr(item, "memory_type", "")),
            "subject": str(getattr(item, "subject", "")),
            "predicate": str(getattr(item, "predicate", "")),
            "value": copy.deepcopy(getattr(item, "value", None)),
            "statement": str(getattr(item, "statement", "")),
            "authority_class": str(getattr(item, "authority_class", "")),
            "tags": list(getattr(item, "tags", ())),
            "sensitivity": str(getattr(item, "sensitivity", "")),
            "source_refs": source_refs,
            "committed_epoch": int(getattr(item, "committed_epoch", 0)),
            "content_hash": str(getattr(item, "content_hash", "")),
        }
        if not record["memory_id"] or record["version"] < 1 or not record["content_hash"]:
            raise ValueError("PROJECT_MEMORY_RESULT_INVALID")
        records.append(record)
    conflicts = []
    for item in tuple(getattr(value, "conflicts", ())):
        conflict = {
            "conflict_id": str(getattr(item, "conflict_id", "")),
            "subject": str(getattr(item, "subject", "")),
            "predicate": str(getattr(item, "predicate", "")),
            "memory_ids": list(getattr(item, "memory_ids", ())),
        }
        if not conflict["conflict_id"] or len(conflict["memory_ids"]) < 2:
            raise ValueError("PROJECT_MEMORY_CONFLICT_INVALID")
        conflicts.append(conflict)
    output = {
        "space_id": str(getattr(value, "space_id", "")),
        "memory_watermark": int(getattr(value, "memory_watermark", -1)),
        "records": records,
        "conflicts": conflicts,
        "excluded_count": int(getattr(value, "excluded_count", 0)),
        "source_set_hash": str(getattr(value, "source_set_hash", "")),
    }
    if not output["space_id"] or output["memory_watermark"] < 0 or not output["source_set_hash"]:
        raise ValueError("PROJECT_MEMORY_RESULT_INVALID")
    return output


def _validate_bundle(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("PROJECT_MEMORY_BUNDLE_INVALID")
    required = (
        "schema_version",
        "bundle_id",
        "run_id",
        "operation",
        "memory_space_id",
        "memory_assignment_hash",
        "source_set_hash",
        "bundle_hash",
        "records",
        "conflicts",
    )
    if value.get("schema_version") != "memory-bundle.v1" or any(not value.get(key) for key in required[:-2]):
        raise ValueError("PROJECT_MEMORY_BUNDLE_INVALID")
    if not isinstance(value.get("records"), list) or not isinstance(value.get("conflicts"), list):
        raise ValueError("PROJECT_MEMORY_BUNDLE_INVALID")
    return copy.deepcopy(dict(value))


def _bounded_strings(value: object) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        output.append(value[:400])
    elif isinstance(value, Mapping):
        for key in sorted(value):
            output.extend(_bounded_strings(value[key]))
            if len(output) >= 20:
                break
    elif isinstance(value, (list, tuple)):
        for item in value[:20]:
            output.extend(_bounded_strings(item))
    return output[:20]


def _safe_segment(value: str) -> str:
    segment = re.sub(r"[^a-z0-9_.-]", "-", value.lower()).strip("-")
    return segment[:60] or "unknown"
