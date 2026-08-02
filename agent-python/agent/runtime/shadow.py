from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Callable, Mapping

from agent.v1 import agent_execution_pb2 as proto

if TYPE_CHECKING:
    from agent.context import RunContext
    from agent.result import AgentResult


class ShadowRemoteEffectError(RuntimeError):
    pass


class NoRemoteEffectsAdapter:
    """Fail-closed Adapter for deterministic shadow evaluation."""

    def __init__(self, adapter_kind: str) -> None:
        self._adapter_kind = adapter_kind

    def __getattr__(self, operation: str):
        def forbidden(*_args, **_kwargs):
            raise ShadowRemoteEffectError(
                f"SHADOW_REMOTE_EFFECT_FORBIDDEN:{self._adapter_kind}:{operation}"
            )

        return forbidden


@dataclass(frozen=True)
class ShadowCounters:
    model_physical_calls: int = 0
    capability_physical_calls: int = 0
    draft_writes: int = 0
    unit_writes: int = 0
    publish_writes: int = 0

    def delta(self, baseline: "ShadowCounters") -> "ShadowCounters":
        return ShadowCounters(
            model_physical_calls=self.model_physical_calls
            - baseline.model_physical_calls,
            capability_physical_calls=self.capability_physical_calls
            - baseline.capability_physical_calls,
            draft_writes=self.draft_writes - baseline.draft_writes,
            unit_writes=self.unit_writes - baseline.unit_writes,
            publish_writes=self.publish_writes - baseline.publish_writes,
        )

    def has_remote_effects(self) -> bool:
        return any(
            value != 0
            for value in (
                self.model_physical_calls,
                self.capability_physical_calls,
                self.draft_writes,
                self.unit_writes,
                self.publish_writes,
            )
        )


@dataclass(frozen=True)
class ShadowEvaluation:
    authoritative_trace_hash: str
    candidate_policy_version: str
    assignment_hash: str
    deterministic_findings: tuple[Mapping[str, object], ...]
    projected_need_requiredness: str
    projected_quality_codes: tuple[str, ...]
    extra_model_physical_calls: int
    extra_capability_physical_calls: int
    extra_draft_writes: int
    extra_unit_writes: int
    extra_publish_writes: int
    evaluator_elapsed_ms: int
    schema_version: str = "shadow-evaluation.v1"


class DeterministicShadowEvaluator:
    def evaluate(
        self,
        *,
        authoritative_trace_hash: str,
        candidate_policy_version: str,
        baseline_counters: ShadowCounters,
        observed_counters: ShadowCounters,
        checks: tuple[Callable[[], Mapping[str, object] | None], ...] = (),
        projected_need_requiredness: str = "",
        projected_quality_codes: tuple[str, ...] = (),
        elapsed_ms: int = 0,
        assignment_hash: str = "",
    ) -> ShadowEvaluation:
        if not authoritative_trace_hash or not candidate_policy_version:
            raise ValueError("shadow evaluation requires trace and policy identities")
        delta = observed_counters.delta(baseline_counters)
        if delta.has_remote_effects():
            raise ShadowRemoteEffectError("SHADOW_REMOTE_EFFECT_FORBIDDEN")
        findings = tuple(result for check in checks if (result := check()) is not None)
        return ShadowEvaluation(
            authoritative_trace_hash=authoritative_trace_hash,
            candidate_policy_version=candidate_policy_version,
            assignment_hash=assignment_hash,
            deterministic_findings=findings,
            projected_need_requiredness=projected_need_requiredness,
            projected_quality_codes=tuple(sorted(set(projected_quality_codes))),
            extra_model_physical_calls=delta.model_physical_calls,
            extra_capability_physical_calls=delta.capability_physical_calls,
            extra_draft_writes=delta.draft_writes,
            extra_unit_writes=delta.unit_writes,
            extra_publish_writes=delta.publish_writes,
            evaluator_elapsed_ms=max(elapsed_ms, 0),
        )


class ShadowArtifactModule:
    """Builds one hash-bound artifact from public authoritative run fields."""

    def build(self, context: "RunContext", result: "AgentResult") -> proto.RunArtifact | None:
        if context.evaluation_mode != "SHADOW":
            return None
        if (
            not context.authoritative_workflow_version
            or context.workflow_version != context.authoritative_workflow_version
            or not context.shadow_workflow_version
            or context.shadow_workflow_version == context.authoritative_workflow_version
            or not context.candidate_policy_version
            or not context.assignment_hash.startswith("sha256:")
        ):
            raise ValueError("shadow evaluation context identity mismatch")
        output = result.run_output
        trace = {
            "run_id": context.run_id,
            "task_id": context.task_id,
            "authoritative_workflow_version": context.authoritative_workflow_version,
            "run_purpose": int(context.run_purpose),
            "checkpoint_sequence": context.checkpoint_sequence,
            "output_key": output.output_key if output is not None else "",
            "output_kind": int(output.output_kind) if output is not None else 0,
            "output_content_hash": output.content_hash if output is not None else "",
            "draft_key": result.draft_key or "",
            "draft_content_hash": (
                "sha256:" + hashlib.sha256(result.draft_patch).hexdigest()
                if result.draft_patch
                else ""
            ),
            "submission_disposition": result.submission_disposition.value,
        }
        trace_content = _canonical_json(trace)
        trace_hash = "sha256:" + hashlib.sha256(trace_content).hexdigest()
        evaluation = DeterministicShadowEvaluator().evaluate(
            authoritative_trace_hash=trace_hash,
            candidate_policy_version=context.candidate_policy_version,
            assignment_hash=context.assignment_hash,
            baseline_counters=ShadowCounters(),
            observed_counters=ShadowCounters(),
            checks=(
                lambda: {
                    "code": "AUTHORITATIVE_TRACE_BOUND",
                    "workflow_version": context.authoritative_workflow_version,
                    "shadow_workflow_version": context.shadow_workflow_version,
                },
            ),
        )
        value = asdict(evaluation)
        value["authoritative_trace"] = trace
        value["deterministic_findings"] = list(evaluation.deterministic_findings)
        value["projected_quality_codes"] = list(evaluation.projected_quality_codes)
        content = _canonical_json(value)
        request_hash = "sha256:" + hashlib.sha256(
            "\x00".join(
                (trace_hash, context.candidate_policy_version, context.assignment_hash)
            ).encode()
        ).hexdigest()
        return proto.RunArtifact(
            artifact_key=f"{context.run_id}:shadow-evaluation:1",
            artifact_type="SHADOW_EVALUATION",
            generation=1,
            request_hash=request_hash,
            content_hash=hashlib.sha256(content).hexdigest(),
            content=content,
        )


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
