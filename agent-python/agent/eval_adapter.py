"""Fold existing Agent runtime events into a content-safe v1 trace payload."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from typing import Any, Mapping

from agent.checkpoint import CheckpointCodec
from agent.result import AgentResult
from agent.runtime import BufferedRuntimeEventSink
from agent.testing.instrumented_gateway import CapabilityObservation
from agent.v1 import agent_execution_pb2 as proto


TRACE_SCHEMA_VERSION = "agent-loop-trace.v1"


class TraceSequenceError(ValueError):
    """Raised when a supposedly complete local trace has missing events."""


@dataclass
class TraceRuntimeEventSink(BufferedRuntimeEventSink):
    """Buffered sink with the same production interface and no extra callbacks."""

    cancelled: bool = False
    private_markers: list[str] = field(default_factory=list, repr=False)


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _token_value(usage: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _fold_attempts(
    events: list[proto.RecordModelAttemptRequest],
) -> list[dict[str, Any]]:
    planned: dict[str, proto.RecordModelAttemptRequest] = {}
    folded: list[dict[str, Any]] = []
    terminal_keys: set[str] = set()
    for event in events:
        if event.status == "PLANNED":
            if event.attempt_key in planned or event.attempt_key in terminal_keys:
                raise TraceSequenceError("duplicate PLANNED model attempt")
            planned[event.attempt_key] = event
            continue
        if event.status not in {"SUCCEEDED", "FAILED"}:
            raise TraceSequenceError(f"unsupported model attempt event: {event.status}")
        start = planned.pop(event.attempt_key, None)
        if start is None:
            raise TraceSequenceError("terminal model attempt has no PLANNED event")
        if start.request_hash != event.request_hash:
            raise TraceSequenceError("model attempt request hash changed")
        if event.attempt_key in terminal_keys:
            raise TraceSequenceError("model attempt has multiple terminal events")
        terminal_keys.add(event.attempt_key)
        metadata = _json_object(event.response_metadata_json)
        usage = _json_object(event.token_usage_json)
        folded.append(
            {
                "attempt_key": event.attempt_key,
                "operation": event.operation,
                "prompt_version": event.prompt_version,
                "provider": event.provider,
                "request_hash": event.request_hash,
                "output_hash": metadata.get("output_hash"),
                "status": event.status,
                "input_tokens": _token_value(usage, "prompt_tokens", "input_tokens", "input"),
                "output_tokens": _token_value(usage, "completion_tokens", "output_tokens", "output"),
                "total_tokens": _token_value(usage, "total_tokens", "total"),
                "latency_ms": _token_value(metadata, "latency_ms"),
                "error_category": event.error_category or None,
                "physical_call": True,
                "durable_replay": False,
                "legacy_incomplete": False,
            }
        )
    if planned:
        raise TraceSequenceError("model attempt has no terminal event")
    return folded


def _decode_checkpoints(
    checkpoints: list[tuple[int, bytes]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    int,
    list[dict[str, Any]],
    dict[str, str],
]:
    codec = CheckpointCodec()
    result: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    previous_sequence = 0
    previous_coverage: dict[str, str] = {}
    previous_evidence_count = 0
    previous_knowledge_counts: dict[str, int | None] = {
        "fact": None,
        "unknown": None,
        "conflict": None,
    }
    replan_count = 0
    actions: list[dict[str, Any]] = []
    for sequence, payload in checkpoints:
        if sequence <= previous_sequence:
            raise TraceSequenceError("checkpoint sequence is duplicated or out of order")
        gap = previous_sequence > 0 and sequence != previous_sequence + 1
        if gap:
            raise TraceSequenceError("checkpoint sequence has a gap")
        decoded = codec.decode(payload)
        if decoded.sequence != sequence:
            raise TraceSequenceError("checkpoint envelope sequence does not match event")
        body = decoded.payload
        snapshot = body.get("snapshot", {})
        if not isinstance(snapshot, dict):
            snapshot = {}
        coverage = snapshot.get("coverage", {})
        if not isinstance(coverage, dict):
            coverage = {}
        evidence_refs = snapshot.get("evidence_refs", [])
        evidence_count = len(evidence_refs) if isinstance(evidence_refs, list) else 0
        knowledge_counts = {
            name: (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )
            for name, value in {
                "fact": snapshot.get("knowledge_fact_count"),
                "unknown": snapshot.get("knowledge_unknown_count"),
                "conflict": snapshot.get("knowledge_conflict_count"),
            }.items()
        }
        for key, after in coverage.items():
            before = previous_coverage.get(str(key), "MISSING")
            if before != str(after):
                transitions.append(
                    {
                        "iteration": max(int(snapshot.get("iteration", 0)), 0),
                        "coverage_key": str(key),
                        "before": before,
                        "after": str(after),
                        "evidence_delta": max(evidence_count - previous_evidence_count, 0),
                        "fact_delta": _count_delta(
                            previous_knowledge_counts["fact"],
                            knowledge_counts["fact"],
                        ),
                        "unknown_delta": _count_delta(
                            previous_knowledge_counts["unknown"],
                            knowledge_counts["unknown"],
                        ),
                        "conflict_delta": _count_delta(
                            previous_knowledge_counts["conflict"],
                            knowledge_counts["conflict"],
                        ),
                        "trigger": str(body.get("status") or body.get("stage") or "CHECKPOINT"),
                    }
                )
        previous_coverage = {str(key): str(value) for key, value in coverage.items()}
        previous_evidence_count = evidence_count
        previous_knowledge_counts = knowledge_counts
        replan_count = max(replan_count, int(snapshot.get("replan_count", 0)))
        if str(body.get("status") or body.get("stage")) == "ACTION_VALIDATED":
            pending = snapshot.get("pending_action", {})
            targets = pending.get("target_coverage", []) if isinstance(pending, dict) else []
            actions.append(
                {
                    "action_signature": str(snapshot.get("action_signature", "")),
                    "target_coverage": [str(item) for item in targets],
                }
            )
        result.append(
            {
                "sequence": sequence,
                "snapshot_schema": str(body.get("snapshot_schema_version", "legacy")),
                "status": str(body.get("status") or body.get("stage") or "UNKNOWN"),
                "payload_bytes": len(payload),
                "content_hash": _hash_bytes(payload),
                "ack_status": "LOCAL",
                "resume_entry": sequence == checkpoints[0][0] and sequence > 1,
                "sequence_gap": False,
            }
        )
        previous_sequence = sequence
    return result, transitions, replan_count, actions, previous_coverage


def _count_delta(before: int | None, after: int | None) -> int | None:
    if after is None:
        return None
    return max(after - (before or 0), 0)


def _grounding_findings(draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in draft.get("grounding_findings", []):
        if not isinstance(raw, Mapping):
            continue
        refs = raw.get("evidence_refs", [])
        facts = raw.get("fact_refs", raw.get("fact_ids", []))
        result.append(
            {
                "claim_id": str(raw.get("claim_id", "unknown")),
                "claim_type": str(raw.get("claim_type", raw.get("kind", "UNKNOWN"))),
                "criticality": str(raw.get("criticality", "UNKNOWN")),
                "status": str(raw.get("status", raw.get("verdict", "UNKNOWN"))),
                "reason_code": str(raw.get("reason_code", "UNSPECIFIED")),
                "fact_ref_count": len(facts) if isinstance(facts, list) else 0,
                "evidence_ref_count": len(refs) if isinstance(refs, list) else 0,
            }
        )
    return result


def _information_need(
    checkpoints: list[tuple[int, bytes]],
) -> dict[str, Any] | None:
    result = None
    codec = CheckpointCodec()
    for _, payload in checkpoints:
        body = codec.decode(payload).payload
        snapshot = body.get("snapshot", {})
        if not isinstance(snapshot, Mapping) or not snapshot.get(
            "effective_requiredness"
        ):
            continue
        coverage = snapshot.get("coverage", {})
        result = {
            "requiredness": str(snapshot["effective_requiredness"]),
            "need_kind": str(snapshot.get("information_need_kind", "UNKNOWN")),
            "route": str(snapshot.get("need_route", "")),
            "reason_code": str(snapshot.get("need_route_reason_code", "")),
            "policy_version": str(
                snapshot.get("information_need_policy_version", "")
            ),
            "coverage_count": len(coverage) if isinstance(coverage, Mapping) else 0,
        }
    return result


def build_trace_payload(
    *,
    sink: TraceRuntimeEventSink,
    capability_observations: list[CapabilityObservation],
    result: AgentResult | None,
    eval_run_id: str,
    case_id: str,
    workflow_version: str,
    execution_mode: str,
    deterministic_only: bool,
    started_at: datetime,
    duration_ms: int,
    input_hash: str,
    failure_category: str | None = None,
    failure_retryable: bool = False,
    incompatibility_reason_code: str | None = None,
) -> dict[str, Any]:
    attempts = _fold_attempts(sink.model_attempts)
    checkpoints, transitions, replan_count, actions, final_coverage = _decode_checkpoints(
        sink.checkpoints
    )
    draft: dict[str, Any] = {}
    output_hash = None
    if result is not None and result.draft_patch:
        output_hash = _hash_bytes(result.draft_patch)
        parsed = json.loads(result.draft_patch)
        if isinstance(parsed, dict):
            draft = parsed
    capabilities = []
    for index, item in enumerate(capability_observations):
        action = actions[index] if index < len(actions) else {}
        capabilities.append({
            "sequence": item.sequence,
            "capability_name": item.capability_name,
            "action_signature": action.get("action_signature") or item.action_signature,
            "target_coverage": action.get("target_coverage", []),
            "status": item.status,
            "evidence_count": item.evidence_count,
            "duration_ms": item.duration_ms,
            "physical_call": item.physical_call,
            "durable_replay": item.durable_replay,
            "error_category": item.error_category,
        })
    artifacts = [
        {
            "artifact_type": item.artifact_type,
            "generation": max(int(item.generation), 0),
            "request_hash": item.request_hash,
            "content_hash": item.content_hash,
            "payload_bytes": len(item.content),
        }
        for item in sink.artifacts
    ]
    tokens = [item["total_tokens"] for item in attempts if item["total_tokens"] is not None]
    evidence_hashes = {
        (item.source_type, item.source_id, item.locator, item.excerpt_hash)
        for item in sink.evidence_items
    }
    failed = failure_category is not None
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "eval_run_id": eval_run_id,
        "case_id": case_id,
        "workflow_version": workflow_version,
        "execution_mode": execution_mode,
        "deterministic_only": deterministic_only,
        "status": "failed" if failed else "completed",
        "started_at": started_at.isoformat(),
        "duration_ms": max(duration_ms, 0),
        "input_hash": input_hash,
        "output_hash": output_hash,
        "result_outcome": draft.get("result_outcome"),
        "stop_reason": draft.get("stop_reason"),
        "resume_entry_status": next(
            (
                item["status"]
                for item in checkpoints
                if item.get("resume_entry")
            ),
            None,
        ),
        "submission_disposition": (
            result.submission_disposition.value if result is not None else None
        ),
        "incompatibility_reason_code": incompatibility_reason_code,
        "information_need": _information_need(sink.checkpoints),
        "model_attempts": attempts,
        "capability_calls": capabilities,
        "coverage_transitions": transitions,
        "grounding_findings": _grounding_findings(draft),
        "artifacts": artifacts,
        "checkpoints": checkpoints,
        "counters": {
            "model_attempt_count": len(attempts),
            "model_physical_call_count": sum(item["physical_call"] for item in attempts),
            "model_durable_replay_count": sum(item["durable_replay"] for item in attempts),
            "capability_call_count": len(capabilities),
            "capability_physical_call_count": sum(item["physical_call"] for item in capabilities),
            "capability_durable_replay_count": sum(item["durable_replay"] for item in capabilities),
            "evidence_count": len(sink.evidence_items),
            "unique_evidence_count": len(evidence_hashes),
            "checkpoint_count": len(checkpoints),
            "total_tokens": sum(tokens) if tokens else None,
            "replan_count": replan_count,
            "coverage_item_count": len(final_coverage),
            "coverage_covered_count": sum(
                value == "COVERED" for value in final_coverage.values()
            ),
        },
        "failure": (
            {
                "category": failure_category,
                "retryable": failure_retryable,
                "public_code": failure_category,
            }
            if failed
            else None
        ),
    }
