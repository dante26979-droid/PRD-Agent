from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    schema_version: str
    workflow_version: str
    run_id: str
    task_id: str
    task_version: int
    status: str
    phase: str
    iteration: int
    tool_call_count: int
    token_usage: int
    replan_count: int
    no_progress_rounds: int
    coverage: dict[str, str]
    active_gap: str | None
    pending_action: dict[str, Any] | None
    action_signature: str | None
    completed_action_signatures: list[str]
    evidence_refs: list[str]
    observations: list[dict[str, str]]
    stop_reason: str | None
    candidate_markdown: str | None
    draft_structured: dict[str, Any]
    draft_hash: str | None
    last_execution_evidence_count: int
    last_model_attempt_key: str | None
    checkpoint_sequence: int
    draft_generation: int
    supplement_count: int
    repair_count: int
    draft_artifact_key: str | None
    draft_artifact_hash: str | None
    draft_bundle: dict[str, Any]
    grounding_artifact_key: str | None
    grounding_outcome: str | None
    grounding_findings: list[dict[str, Any]]
    quality_artifact_key: str | None
    quality_outcome: str | None
    quality_issues: list[dict[str, Any]]
    confirmation_units: list[dict[str, Any]]
    confirmation_artifact_key: str | None
    immutable_unit_keys: list[str]
