from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Callable, Mapping

from agent.context import RunContext
from agent.runtime import BudgetVector, LedgerCallSpec, RunExecutionLedger
from agent.runtime.idempotency import canonical_json, request_hash, tagged_sha256

from .artifact import validate_context_pack_value
from .estimator import ConservativeTokenEstimator, TokenEstimator
from .models import (
    ContextPack,
    ContextSource,
    PreparedModelContext,
    SourceManifest,
    TokenAccounting,
)
from .policy import ContextPolicy


class ContextWindowUnsatisfiable(ValueError):
    pass


class ContextCompactionOutputInvalid(ValueError):
    pass


class ContextCompactionBudgetUnavailable(ValueError):
    pass


SemanticCompactor = Callable[[Mapping[str, object]], Mapping[str, object]]

_MANDATORY_BY_OPERATION = {
    "plan_information_need": {
        "task_id",
        "task_version",
        "task_message",
        "workflow_version",
        "repository_available",
        "historical_prd_available",
        "revision_scope",
        "allowed_requiredness",
        "allowed_need_kinds",
        "allowed_source_types",
    },
    "select_investigation_action": {
        "run_id",
        "task_id",
        "task_message",
        "workflow_version",
        "active_coverage_gap",
        "coverage",
        "remaining_budget",
        "instruction",
    },
    "generate_working_draft": {
        "run_id",
        "task_id",
        "task_message",
        "coverage",
        "evidence",
        "stop_reason",
    },
    "repair_working_draft": {
        "run_id",
        "task_id",
        "markdown",
        "quality_issues",
        "instruction",
    },
    "plan_outline": {
        "unit_operation",
        "expected_schema",
        "task_message",
        "unit_scope",
        "output_contract",
        "instruction",
    },
    "generate_unit": {
        "unit_operation",
        "expected_schema",
        "task_message",
        "unit_scope",
        "output_contract",
        "instruction",
    },
    "revise_unit": {
        "unit_operation",
        "expected_schema",
        "task_message",
        "unit_scope",
        "base_candidate",
        "output_contract",
        "instruction",
    },
}

_SOURCE_PRIORITY = {
    "task_message": 0,
    "unit_scope": 0,
    "base_candidate": 0,
    "quality_issues": 0,
    "remaining_budget": 0,
    "coverage": 1,
    "markdown": 1,
    "project_memory": 1,
    "evidence": 2,
    "observations": 2,
    "prior_actions": 3,
}


@dataclass(frozen=True)
class _Draft:
    manifest: SourceManifest
    original: Mapping[str, object]
    reduced: Mapping[str, object]
    omitted: tuple[Mapping[str, str], ...]
    before_tokens: int
    after_tokens: int
    mandatory_keys: frozenset[str]
    compaction_kind: str


class ContextPackModule:
    def __init__(
        self,
        policy: ContextPolicy = ContextPolicy(),
        estimator: TokenEstimator | None = None,
    ) -> None:
        self.policy = policy
        self.estimator = estimator or ConservativeTokenEstimator()

    def prepare(
        self,
        *,
        context: RunContext,
        ledger: RunExecutionLedger | None,
        operation: str,
        operation_sequence: int,
        prompt_version: str = "",
        output_schema: str = "model-response.v1",
        system_prompt: str,
        payload: Mapping[str, object],
        semantic_compactor: SemanticCompactor | None = None,
        base_request_hash: str | None = None,
    ) -> PreparedModelContext:
        original = _mapping_copy(payload)
        original_bytes = canonical_json(original)
        original_hash = base_request_hash or tagged_sha256(original_bytes)
        if self.policy.mode == "off":
            return PreparedModelContext(
                payload=original,
                canonical_payload=original_bytes,
                request_hash=original_hash,
                estimated_input_tokens=self.estimator.estimate_request(
                    system_prompt, original_bytes
                ),
            )
        if ledger is None:
            raise ValueError("CONTEXT_LEDGER_REQUIRED")

        draft = self._build_draft(context, operation, system_prompt, original)
        compaction_required = draft.compaction_kind != "NONE"
        if (
            compaction_required
            and draft.after_tokens > self.policy.target_input_tokens
            and self.policy.semantic_compaction_enabled
            and semantic_compactor is not None
            and ledger.remaining().model_attempts >= 2
        ):
            request = {
                "schema_version": "context-compaction-request.v1",
                "operation": operation,
                "output_schema": output_schema,
                "target_input_tokens": self.policy.target_input_tokens,
                "mandatory_keys": sorted(draft.mandatory_keys),
                "allowed_source_refs": [
                    item.source_ref for item in draft.manifest.sources
                ],
                "operation_view": draft.reduced,
                "instruction": (
                    "Return an operation_view object only. Preserve mandatory fields "
                    "exactly, do not create identifiers, facts, decisions, unknowns, "
                    "conflicts, or source references."
                ),
            }
            try:
                candidate = semantic_compactor(request)
            except ContextCompactionBudgetUnavailable:
                candidate = None
            if candidate is not None:
                reduced = self.validate_semantic_view(
                    candidate,
                    original=draft.reduced,
                    mandatory_keys=draft.mandatory_keys,
                )
                after = self.estimator.estimate_request(
                    system_prompt, canonical_json(reduced)
                )
                draft = _Draft(
                    manifest=draft.manifest,
                    original=draft.original,
                    reduced=reduced,
                    omitted=draft.omitted,
                    before_tokens=draft.before_tokens,
                    after_tokens=after,
                    mandatory_keys=draft.mandatory_keys,
                    compaction_kind="SEMANTIC",
                )

        if (
            compaction_required
            and draft.after_tokens > self.policy.target_input_tokens
        ) or draft.after_tokens > self.policy.hard_input_tokens:
            draft = self._emergency_reduce(draft, system_prompt)
        if draft.after_tokens > self.policy.hard_input_tokens:
            raise ContextWindowUnsatisfiable("CONTEXT_WINDOW_UNSATISFIABLE")

        pack = self._pack(
            context,
            operation,
            operation_sequence,
            prompt_version,
            output_schema,
            draft,
        )
        pack_value = pack.as_dict()
        spec = LedgerCallSpec(
            entry_kind="LOCAL_DERIVATION",
            operation="build_context_pack",
            operation_key=(
                f"context:build:{_safe_segment(operation)}:sequence{operation_sequence}"
            ),
            request_hash=request_hash(
                {
                    "operation": operation,
                    "operation_sequence": operation_sequence,
                    "prompt_version": prompt_version,
                    "output_schema": output_schema,
                    "policy_version": self.policy.version,
                    "source_manifest_hash": pack.source_manifest.manifest_hash,
                    "target_input_tokens": self.policy.target_input_tokens,
                    "operation_view_hash": request_hash(pack.operation_view),
                }
            ),
            reservation=BudgetVector(),
            outcome_schema="context-pack.v1",
        )
        outcome = ledger.execute_derivation(
            spec,
            lambda: pack_value,
            validate_context_pack_value,
            artifact_type="CONTEXT_PACK",
        )
        effective = original
        if self.policy.mode == "enforce":
            effective = {
                **dict(pack.operation_view),
                "context_pack": {
                    "schema_version": "context-pack-ref.v1",
                    "pack_id": pack.pack_id,
                    "artifact_key": outcome.artifact.artifact_key,
                    "content_hash": outcome.artifact.content_hash,
                    "source_manifest_hash": pack.source_manifest.manifest_hash,
                    "policy_version": self.policy.version,
                },
            }
        effective_bytes = canonical_json(effective)
        estimated = self.estimator.estimate_request(system_prompt, effective_bytes)
        if self.policy.mode == "enforce" and estimated > self.policy.hard_input_tokens:
            raise ContextWindowUnsatisfiable("CONTEXT_WINDOW_UNSATISFIABLE")
        identity = request_hash(
            {
                "base_request_hash": original_hash,
                "context_policy_version": self.policy.version,
                "context_pack_hash": outcome.artifact.content_hash,
                "effective_payload_hash": tagged_sha256(effective_bytes),
            }
        )
        return PreparedModelContext(
            payload=effective,
            canonical_payload=effective_bytes,
            request_hash=identity if self.policy.mode == "enforce" else original_hash,
            estimated_input_tokens=estimated,
            context_pack=pack,
            context_pack_artifact_key=outcome.artifact.artifact_key,
            context_pack_artifact_hash=outcome.artifact.content_hash,
            shadow_payload=pack.operation_view if self.policy.mode == "shadow" else None,
        )

    def _build_draft(
        self,
        context: RunContext,
        operation: str,
        system_prompt: str,
        original: Mapping[str, object],
    ) -> _Draft:
        mandatory = frozenset(
            key for key in _MANDATORY_BY_OPERATION.get(operation, ()) if key in original
        )
        sources = tuple(
            self._source(context, operation, key, value, key in mandatory)
            for key, value in sorted(original.items())
        )
        manifest_without_hash = {
            "schema_version": "context-source-manifest.v1",
            "run_id": context.run_id,
            "task_id": context.task_id,
            "operation": operation,
            "sources": [item.manifest_value() for item in sources],
        }
        manifest = SourceManifest(
            schema_version="context-source-manifest.v1",
            run_id=context.run_id,
            task_id=context.task_id,
            operation=operation,
            sources=sources,
            manifest_hash=request_hash(manifest_without_hash),
        )
        before = self.estimator.estimate_request(system_prompt, canonical_json(original))
        if before <= self.policy.compact_threshold_tokens:
            return _Draft(
                manifest,
                original,
                original,
                (),
                before,
                before,
                mandatory,
                "NONE",
            )
        reduced, omitted = self._deterministic_reduce(operation, original, mandatory)
        after = self.estimator.estimate_request(system_prompt, canonical_json(reduced))
        return _Draft(
            manifest,
            original,
            reduced,
            tuple(omitted),
            before,
            after,
            mandatory,
            "DETERMINISTIC",
        )

    def _source(
        self,
        context: RunContext,
        operation: str,
        key: str,
        value: object,
        required: bool,
    ) -> ContextSource:
        digest = request_hash(value)
        return ContextSource(
            source_ref=f"run:{context.run_id}:{operation}:{key}",
            source_kind=key.upper(),
            content_hash=digest,
            trust_class=_trust_class(key),
            required=required,
            priority=_SOURCE_PRIORITY.get(key, 2),
            value=value,
        )

    def _deterministic_reduce(
        self,
        operation: str,
        original: Mapping[str, object],
        mandatory: frozenset[str],
    ) -> tuple[dict[str, object], list[Mapping[str, str]]]:
        reduced = _mapping_copy(original)
        omitted: list[Mapping[str, str]] = []
        scope = reduced.get("unit_scope")
        if isinstance(scope, Mapping):
            reduced_scope, scope_omitted = _reduce_unit_scope(scope, operation)
            reduced["unit_scope"] = reduced_scope
            omitted.extend(scope_omitted)
        for key, limit in (
            ("observations", self.policy.max_observations),
            ("evidence", self.policy.max_observations),
            ("prior_actions", self.policy.max_prior_actions),
        ):
            value = reduced.get(key)
            if isinstance(value, list):
                unique = _dedupe_items(value)
                if len(unique) > limit:
                    omitted.append(
                        {
                            "source_ref": key,
                            "reason": "OLDER_ITEMS_OUTSIDE_OPERATION_WINDOW",
                        }
                    )
                reduced[key] = unique[-limit:]
        for key, value in tuple(reduced.items()):
            if key in mandatory:
                continue
            reduced[key] = _bound_optional_strings(
                value, self.policy.max_optional_string_chars
            )
        return reduced, omitted

    def _emergency_reduce(self, draft: _Draft, system_prompt: str) -> _Draft:
        reduced = _mapping_copy(draft.reduced)
        omitted = list(draft.omitted)
        optional = sorted(
            (key for key in reduced if key not in draft.mandatory_keys),
            key=lambda key: (_SOURCE_PRIORITY.get(key, 2), key),
            reverse=True,
        )
        for key in optional:
            if self.estimator.estimate_request(
                system_prompt, canonical_json(reduced)
            ) <= self.policy.target_input_tokens:
                break
            reduced.pop(key, None)
            omitted.append(
                {"source_ref": key, "reason": "EMERGENCY_OPTIONAL_OMISSION"}
            )
        after = self.estimator.estimate_request(system_prompt, canonical_json(reduced))
        return _Draft(
            draft.manifest,
            draft.original,
            reduced,
            tuple(omitted),
            draft.before_tokens,
            after,
            draft.mandatory_keys,
            (
                "SEMANTIC_AND_EMERGENCY"
                if draft.compaction_kind == "SEMANTIC"
                else "DETERMINISTIC_EMERGENCY"
            ),
        )

    def _pack(
        self,
        context: RunContext,
        operation: str,
        operation_sequence: int,
        prompt_version: str,
        output_schema: str,
        draft: _Draft,
    ) -> ContextPack:
        seed = {
            "run_id": context.run_id,
            "task_id": context.task_id,
            "operation": operation,
            "operation_sequence": operation_sequence,
            "prompt_version": prompt_version,
            "output_schema": output_schema,
            "policy_version": self.policy.version,
            "manifest_hash": draft.manifest.manifest_hash,
            "view_hash": request_hash(draft.reduced),
        }
        return ContextPack(
            schema_version="context-pack.v1",
            pack_id="context-" + request_hash(seed).removeprefix("sha256:")[:24],
            run_id=context.run_id,
            task_id=context.task_id,
            operation=operation,
            operation_sequence=operation_sequence,
            prompt_version=prompt_version,
            output_schema=output_schema,
            policy_version=self.policy.version,
            source_manifest=draft.manifest,
            operation_view=draft.reduced,
            omitted_sources=draft.omitted,
            token_accounting=TokenAccounting(
                estimated_tokens_before=draft.before_tokens,
                estimated_tokens_after=draft.after_tokens,
                target_input_tokens=self.policy.target_input_tokens,
                hard_input_tokens=self.policy.hard_input_tokens,
                reserved_output_tokens=self.policy.reserved_output_tokens,
                emergency_margin_tokens=self.policy.emergency_margin_tokens,
            ),
            compaction_kind=draft.compaction_kind,
        )

    @staticmethod
    def validate_semantic_view(
        value: object,
        *,
        original: Mapping[str, object],
        mandatory_keys: frozenset[str],
    ) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise ContextCompactionOutputInvalid(
                "CONTEXT_COMPACTION_OUTPUT_INVALID"
            )
        raw_view = value.get("operation_view", value)
        if not isinstance(raw_view, Mapping):
            raise ContextCompactionOutputInvalid(
                "CONTEXT_COMPACTION_OUTPUT_INVALID"
            )
        view = _mapping_copy(raw_view)
        for key in mandatory_keys:
            if key not in view or view[key] != original.get(key):
                raise ContextCompactionOutputInvalid(
                    "CONTEXT_COMPACTION_MANDATORY_SOURCE_CHANGED"
                )
        allowed_ids = _collect_ids(original)
        if not _collect_ids(view).issubset(allowed_ids):
            raise ContextCompactionOutputInvalid("CONTEXT_COMPACTION_NEW_ID")
        if not _is_extractive_subset(view, original):
            raise ContextCompactionOutputInvalid("CONTEXT_COMPACTION_NEW_CONTENT")
        return view


def _mapping_copy(value: Mapping[str, object]) -> dict[str, object]:
    return copy.deepcopy(dict(value))


def _safe_segment(value: str) -> str:
    segment = re.sub(r"[^a-z0-9_.-]", "-", value.lower()).strip("-")
    return segment[:60] or "unknown"


def _trust_class(key: str) -> str:
    if key in {"task_message", "quality_issues"}:
        return "USER_INTENT"
    if key in {"unit_scope", "base_candidate"}:
        return "USER_CONFIRMED"
    if key in {"evidence", "observations"}:
        return "DOCUMENT_SUPPORTED"
    if key == "project_memory":
        return "PROJECT_MEMORY"
    return "RUNTIME_METADATA"


def _reduce_unit_scope(
    scope: Mapping[str, object], operation: str
) -> tuple[dict[str, object], list[Mapping[str, str]]]:
    reduced = _mapping_copy(scope)
    confirmed = reduced.get("confirmed_context")
    if not isinstance(confirmed, list):
        return reduced, []
    dependencies = {
        str(item) for item in reduced.get("dependency_unit_keys", [])
    }
    current = str(reduced.get("current_unit_key") or "")
    keep_full = dependencies | ({current} if operation == "revise_unit" else set())
    output: list[object] = []
    omitted: list[Mapping[str, str]] = []
    for raw in confirmed:
        if not isinstance(raw, Mapping):
            continue
        item = _mapping_copy(raw)
        unit_key = str(item.get("unit_key") or "")
        if unit_key not in keep_full and item.get("markdown"):
            item["markdown"] = ""
            omitted.append(
                {
                    "source_ref": f"confirmation-unit:{unit_key}",
                    "reason": "NON_DEPENDENCY_BODY_EXTERNALIZED",
                }
            )
        output.append(item)
    reduced["confirmed_context"] = output
    return reduced, omitted


def _dedupe_items(values: list[object]) -> list[object]:
    output: list[object] = []
    seen: set[str] = set()
    for value in values:
        identity = request_hash(value)
        if identity in seen:
            continue
        seen.add(identity)
        output.append(value)
    return output


def _bound_optional_strings(value: object, limit: int) -> object:
    if isinstance(value, str) and len(value) > limit:
        head = max(limit * 2 // 3, 1)
        tail = max(limit - head, 0)
        suffix = value[-tail:] if tail else ""
        return value[:head] + "\n[...OMITTED_BY_CONTEXT_POLICY...]\n" + suffix
    if isinstance(value, list):
        return [_bound_optional_strings(item, limit) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _bound_optional_strings(item, limit)
            for key, item in value.items()
        }
    return value


def _collect_ids(value: object, key: str = "") -> set[str]:
    output: set[str] = set()
    if isinstance(value, Mapping):
        for nested_key, nested in value.items():
            output.update(_collect_ids(nested, str(nested_key)))
    elif isinstance(value, list):
        for item in value:
            output.update(_collect_ids(item, key))
    elif isinstance(value, str) and (
        key.endswith("_id")
        or key.endswith("_ids")
        or key.endswith("_refs")
        or key in {"unit_key", "node_key"}
    ):
        output.add(value)
    return output


def _is_extractive_subset(candidate: object, original: object) -> bool:
    """Allow semantic selection/omission, never ungrounded rewriting.

    A mapping may omit keys, and a list may omit complete items. Retained scalar
    values and list items must be byte-for-byte values already present in the
    deterministic operation view. This is deliberately stricter than asking a
    model to self-certify that a newly written summary contains no new facts.
    """

    if isinstance(candidate, Mapping):
        if not isinstance(original, Mapping):
            return False
        return all(
            key in original and _is_extractive_subset(value, original[key])
            for key, value in candidate.items()
        )
    if isinstance(candidate, list):
        if not isinstance(original, list):
            return False
        return all(any(item == source for source in original) for item in candidate)
    return candidate == original
