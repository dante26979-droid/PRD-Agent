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
    model_attempt_count: int
    tool_call_count: int
    token_usage: int
    replan_count: int
    no_progress_rounds: int
    coverage: dict[str, str]
    active_gap: str | None
    pending_action: dict[str, Any] | None
    action_signature: str | None
    completed_action_signatures: list[str]
    action_history: list[dict[str, Any]]
    evidence_refs: list[str]
    observations: list[dict[str, str]]
    stop_reason: str | None
    candidate_markdown: str | None
    draft_structured: dict[str, Any]
    draft_hash: str | None
    last_execution_evidence_count: int
    last_knowledge_progressed: bool
    last_model_attempt_key: str | None
    checkpoint_sequence: int
    draft_generation: int
    supplement_count: int
    repair_count: int
    draft_artifact_key: str | None
    draft_artifact_hash: str | None
    draft_artifact_type: str | None
    draft_artifact_generation: int
    draft_artifact_request_hash: str | None
    draft_bundle: dict[str, Any]
    grounding_artifact_key: str | None
    grounding_artifact_hash: str | None
    grounding_artifact_type: str | None
    grounding_artifact_generation: int
    grounding_artifact_request_hash: str | None
    grounding_outcome: str | None
    grounding_findings: list[dict[str, Any]]
    quality_artifact_key: str | None
    quality_artifact_hash: str | None
    quality_artifact_type: str | None
    quality_artifact_generation: int
    quality_artifact_request_hash: str | None
    quality_outcome: str | None
    quality_issues: list[dict[str, Any]]
    confirmation_units: list[dict[str, Any]]
    confirmation_artifact_key: str | None
    confirmation_artifact_hash: str | None
    confirmation_artifact_type: str | None
    confirmation_artifact_generation: int
    confirmation_artifact_request_hash: str | None
    immutable_unit_keys: list[str]
    reopened_unit_keys: list[str]
    run_purpose: str
    unit_scope: dict[str, Any]
    unit_scope_hash: str
    outline_artifact_key: str | None
    outline_artifact_hash: str | None
    outline_artifact_type: str | None
    outline_artifact_generation: int
    outline_artifact_request_hash: str | None
    unit_artifact_key: str | None
    unit_artifact_hash: str | None
    unit_artifact_type: str | None
    unit_artifact_generation: int
    unit_artifact_request_hash: str | None
    full_review_artifact_key: str | None
    full_review_artifact_hash: str | None
    full_review_artifact_type: str | None
    full_review_artifact_generation: int
    full_review_artifact_request_hash: str | None
    information_need_plan_id: str
    information_need_context_hash: str
    information_need_artifact_key: str
    information_need_artifact_hash: str
    information_need_artifact_type: str
    information_need_artifact_generation: int
    information_need_artifact_request_hash: str
    information_need_policy_version: str
    information_need_kind: str
    effective_requiredness: str
    need_route: str
    need_route_reason_code: str
    knowledge_bundle_id: str
    knowledge_fact_count: int
    knowledge_unknown_count: int
    knowledge_conflict_count: int
    knowledge_fingerprint: str
    progress_fingerprint: str
    knowledge_artifact_key: str
    knowledge_artifact_hash: str
    knowledge_artifact_type: str
    knowledge_artifact_generation: int
    knowledge_artifact_request_hash: str
