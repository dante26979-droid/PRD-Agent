from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable, Mapping

from langgraph.graph import END, START, StateGraph

from agent.checkpoint import CheckpointCodec, CheckpointError
from agent.context import RunContext
from agent.evidence import repository_hits_to_evidence
from agent.investigation.models import (
    CoverageStatus,
    InvestigationBudget,
    ProposedAction,
    StopReason,
)
from agent.investigation.policies import (
    DuplicateActionError,
    next_gap,
    pre_action_stop,
    validate_action,
)
from agent.model import ModelApiError, ModelResponse
from agent.quality import DraftQualityPolicy
from agent.result import AgentResult
from agent.runtime import BufferedRuntimeEventSink, RuntimeEventSink
from agent.v1 import agent_execution_pb2 as proto

from .snapshot import LoopCheckpointStatus, LoopSnapshot
from .state import AgentState
from .advanced import AdvancedLoopRunner


@dataclass(frozen=True)
class LangGraphAgentLoop:
    """Bounded model-capability loop implemented as a LangGraph state graph."""

    model: object
    checkpoint_codec: CheckpointCodec
    quality_policy: DraftQualityPolicy
    capability_factory: Callable[[RunContext], object] | None = None
    budget: InvestigationBudget = InvestigationBudget()
    required_coverage: tuple[str, ...] = ("repository_evidence",)
    advanced_loop_mode: str = "off"
    max_supplements: int = 1
    max_quality_repairs: int = 1

    def __call__(self, context: RunContext, cancel_event=None) -> AgentResult:
        _raise_if_cancelled(cancel_event)
        state = self._initial_state(context)
        local_sink = BufferedRuntimeEventSink()
        sink: RuntimeEventSink = context.event_sink or local_sink
        gateway = None
        evidence_items: list[proto.EvidenceItem] = []
        try:
            if self.capability_factory is not None:
                gateway = self.capability_factory(context)
            if not _is_advanced_status(state.get("status")):
                graph = self._build_graph(
                    context=context,
                    cancel_event=cancel_event,
                    gateway=gateway,
                    sink=sink,
                    evidence_items=evidence_items,
                )
                state = graph.invoke(state)
            if (
                self.advanced_loop_mode in {"shadow", "enforce"}
                and state.get("status")
                != LoopCheckpointStatus.READY_TO_SUBMIT.value
            ):
                supplement = None
                if gateway is not None:
                    supplement = lambda query: self._execute_supplement(
                        context, gateway, query
                    )
                advanced_sink: RuntimeEventSink = (
                    sink
                    if self.advanced_loop_mode == "enforce"
                    else BufferedRuntimeEventSink()
                )
                advanced = AdvancedLoopRunner(
                    context=context,
                    sink=advanced_sink,
                    emit_checkpoint=lambda current, status: self._emit_checkpoint(
                        context, current, advanced_sink, status
                    ),
                    generate_draft=lambda current: self._generate_draft(
                        context, current, advanced_sink, cancel_event
                    ),
                    repair_draft=lambda current, issues: self._repair_draft(
                        context,
                        current,
                        issues,
                        advanced_sink,
                        cancel_event,
                    ),
                    supplement=supplement,
                    max_supplements=self.max_supplements,
                    max_repairs=self.max_quality_repairs,
                )
                advanced_state = advanced.invoke(state)
                if self.advanced_loop_mode == "enforce":
                    state = advanced_state
            restored_ready_snapshot = (
                state.get("status") == LoopCheckpointStatus.READY_TO_SUBMIT.value
            )
            markdown = state.get("candidate_markdown")
            if not isinstance(markdown, str) or not markdown.strip():
                if (
                    state.get("stop_reason")
                    == StopReason.TOKEN_BUDGET_EXHAUSTED.value
                ):
                    markdown = (
                        "# PRD Working Draft\n\n"
                        "## 需求\n\n"
                        f"{context.task_message.strip()}\n\n"
                        "## 调查结果\n\n"
                        "模型 Token 预算已耗尽；当前实现事实保留为 Unknown，"
                        "需要后续补充调查或用户确认。"
                    )
                    tokens = 0
                else:
                    markdown, tokens = self._generate_draft(
                        context,
                        state,
                        sink,
                        cancel_event,
                    )
                state["token_usage"] = int(state.get("token_usage", 0)) + tokens
            if int(state.get("token_usage", 0)) > self.budget.token_budget:
                raise ValueError("Agent Run token budget exceeded")
            markdown = self.quality_policy.validate(markdown)
            if restored_ready_snapshot:
                if state.get("draft_hash") != _sha256_text(markdown):
                    raise CheckpointError("READY_TO_SUBMIT snapshot draft hash mismatch")
            else:
                state["candidate_markdown"] = markdown
                state["draft_hash"] = _sha256_text(markdown)
                state["phase"] = LoopCheckpointStatus.READY_TO_SUBMIT.value
                state["checkpoint_sequence"] = self._emit_checkpoint(
                    context,
                    state,
                    sink,
                    LoopCheckpointStatus.READY_TO_SUBMIT,
                )
                state["status"] = LoopCheckpointStatus.READY_TO_SUBMIT.value

            coverage = state.get("coverage", {})
            if coverage and all(
                value == CoverageStatus.COVERED.value for value in coverage.values()
            ):
                result_outcome = "DRAFT_READY"
            elif state.get("evidence_refs"):
                result_outcome = "PARTIAL_EVIDENCE"
            else:
                result_outcome = "EMPTY_EVIDENCE"
            draft_payload = {
                    "task_id": context.task_id,
                    "run_id": context.run_id,
                    "markdown": markdown,
                    "result_outcome": result_outcome,
                    "coverage": coverage,
                    "evidence_refs": state.get("evidence_refs", []),
                    "stop_reason": state.get("stop_reason"),
                }
            if self.advanced_loop_mode == "enforce":
                draft_payload.update(
                    {
                        "schema_version": "working-draft.v2",
                        "draft_generation": state.get("draft_generation", 1),
                        "draft_artifact_key": state.get("draft_artifact_key"),
                        "draft_artifact_hash": state.get("draft_artifact_hash"),
                        "grounding_outcome": state.get("grounding_outcome"),
                        "grounding_findings": state.get("grounding_findings", []),
                        "quality_outcome": state.get("quality_outcome"),
                        "quality_issues": state.get("quality_issues", []),
                        "confirmation_units": state.get("confirmation_units", []),
                    }
                )
            draft_patch = json.dumps(
                draft_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            draft_generation = max(int(state.get("checkpoint_sequence", 1)), 1)

            if context.event_sink is not None:
                return AgentResult(
                    draft_key=f"{context.run_id}:draft:{draft_generation}",
                    expected_task_version=max(context.task_version, 1),
                    draft_patch=draft_patch,
                )

            completed = [
                item for item in local_sink.model_attempts if item.status != "PLANNED"
            ]
            if local_sink.checkpoints:
                checkpoint_sequence, checkpoint = local_sink.checkpoints[-1]
            elif restored_ready_snapshot and context.checkpoint:
                checkpoint_sequence, checkpoint = (
                    context.checkpoint_sequence,
                    context.checkpoint,
                )
            else:
                raise ValueError("Agent graph completed without a checkpoint")
            return AgentResult(
                attempt=completed[0] if completed else None,
                additional_attempts=tuple(completed[1:]),
                evidence=tuple(local_sink.evidence_items),
                checkpoint_sequence=checkpoint_sequence,
                checkpoint=checkpoint,
                draft_key=f"{context.run_id}:draft:{draft_generation}",
                expected_task_version=max(context.task_version, 1),
                draft_patch=draft_patch,
            )
        finally:
            close = getattr(gateway, "close", None)
            if callable(close):
                close()

    def _build_graph(
        self,
        *,
        context: RunContext,
        cancel_event,
        gateway,
        sink: RuntimeEventSink,
        evidence_items: list[proto.EvidenceItem],
    ):
        builder = StateGraph(AgentState)

        def resume(state: AgentState):
            return {}

        def route_resume(state: AgentState) -> str:
            status = state.get("status")
            if status == LoopCheckpointStatus.ACTION_VALIDATED.value:
                return "execute"
            if status in {
                LoopCheckpointStatus.OBSERVED.value,
                LoopCheckpointStatus.INITIALIZED.value,
            }:
                return "assess"
            if status in {
                LoopCheckpointStatus.INVESTIGATION_FINISHED.value,
                LoopCheckpointStatus.READY_TO_SUBMIT.value,
            }:
                return "done"
            raise CheckpointError(f"unsupported loop checkpoint status: {status}")

        def assess_gap(state: AgentState):
            _raise_if_cancelled(cancel_event)
            reason = pre_action_stop(state, self.budget)
            coverage = state.get("coverage", {})
            return {
                "active_gap": None if reason else next_gap(coverage),
                "stop_reason": reason.value if reason else None,
                "phase": "ASSESSED",
            }

        def route_assessment(state: AgentState) -> str:
            return "finish" if state.get("stop_reason") else "select"

        def select_action(state: AgentState):
            _raise_if_cancelled(cancel_event)
            iteration = int(state.get("iteration", 0)) + 1
            gap = str(state.get("active_gap") or "")
            payload = {
                "run_id": context.run_id,
                "task_id": context.task_id,
                "task_message": context.task_message,
                "workflow_version": context.workflow_version,
                "active_coverage_gap": gap,
                "coverage": state.get("coverage", {}),
                "observations": state.get("observations", []),
                "instruction": (
                    "Return exactly one action, or return markdown only when no "
                    "capability evidence is needed."
                ),
            }
            if context.revision_scope is not None:
                payload["revision_scope"] = {
                    "base_draft_id": context.revision_scope.base_draft_id,
                    "reopened_unit_keys": list(
                        context.revision_scope.reopened_unit_keys
                    ),
                    "immutable_unit_keys": list(
                        context.revision_scope.immutable_unit_keys
                    ),
                    "user_feedback": context.revision_scope.user_feedback,
                }
            response, attempt = self._call_model(
                context,
                sink,
                operation="plan_or_generate_working_draft",
                attempt_sequence=iteration,
                payload=payload,
                cancel_event=cancel_event,
            )
            structured = _structured_output(response)
            tokens = _total_tokens(response)
            markdown = structured.get("markdown")
            if isinstance(markdown, str) and markdown.strip():
                return {
                    "iteration": iteration,
                    "token_usage": int(state.get("token_usage", 0)) + tokens,
                    "candidate_markdown": markdown,
                    "draft_structured": structured,
                    "pending_action": None,
                    "stop_reason": StopReason.DRAFT_READY.value,
                    "phase": "INVESTIGATION_FINISHED",
                }
            action = ProposedAction.from_model_output(structured, active_gap=gap)
            total_usage = int(state.get("token_usage", 0)) + tokens
            return {
                "iteration": iteration,
                "token_usage": total_usage,
                "pending_action": action.as_dict(),
                "stop_reason": (
                    StopReason.TOKEN_BUDGET_EXHAUSTED.value
                    if total_usage >= self.budget.token_budget
                    else None
                ),
                "phase": "ACTION_SELECTED",
                "last_model_attempt_key": attempt.attempt_key,
            }

        def route_selection(state: AgentState) -> str:
            if state.get("candidate_markdown"):
                return "finish"
            if int(state.get("token_usage", 0)) >= self.budget.token_budget:
                return "finish"
            return "validate"

        def validate_selected_action(state: AgentState):
            raw = state.get("pending_action")
            if not isinstance(raw, Mapping):
                raise ValueError("Agent graph has no pending action")
            action = ProposedAction(
                tool_id=str(raw["tool_id"]),
                tool_schema_version=str(raw.get("tool_schema_version", "1")),
                arguments=dict(raw["arguments"]),
                purpose=str(raw["purpose"]),
                target_coverage=tuple(raw["target_coverage"]),
            )
            try:
                signature = validate_action(
                    action,
                    active_gap=str(state.get("active_gap") or ""),
                    completed_signatures=tuple(
                        state.get("completed_action_signatures", [])
                    ),
                )
            except DuplicateActionError:
                replans = int(state.get("replan_count", 0))
                return {
                    "pending_action": None,
                    "action_signature": None,
                    "no_progress_rounds": int(
                        state.get("no_progress_rounds", 0)
                    )
                    + 1,
                    "replan_count": min(replans + 1, self.budget.max_replans),
                    "phase": "ASSESSED",
                }
            return {
                "action_signature": signature,
                "phase": "ACTION_VALIDATED",
            }

        def route_validation(state: AgentState) -> str:
            return "checkpoint_action" if state.get("action_signature") else "assess"

        def checkpoint_action(state: AgentState):
            sequence = self._emit_checkpoint(
                context,
                state,
                sink,
                LoopCheckpointStatus.ACTION_VALIDATED,
            )
            return {
                "checkpoint_sequence": sequence,
                "status": LoopCheckpointStatus.ACTION_VALIDATED.value,
                "phase": LoopCheckpointStatus.ACTION_VALIDATED.value,
            }

        def execute_capability(state: AgentState):
            _raise_if_cancelled(cancel_event)
            if gateway is None:
                raise ValueError("investigation requires Capability Gateway")
            raw = state.get("pending_action")
            if not isinstance(raw, Mapping):
                raise ValueError("validated action is missing")
            action = ProposedAction(
                tool_id=str(raw["tool_id"]),
                tool_schema_version=str(raw.get("tool_schema_version", "1")),
                arguments=dict(raw["arguments"]),
                purpose=str(raw["purpose"]),
                target_coverage=tuple(raw["target_coverage"]),
            )
            items = self._execute_action(context, gateway, action)
            if items:
                sink.evidence(items)
                evidence_items.extend(items)
            observations = list(state.get("observations", []))
            references = list(state.get("evidence_refs", []))
            for item in items:
                reference = _evidence_reference(item)
                if reference not in references:
                    references.append(reference)
                    observations.append(
                        {
                            "source_type": item.source_type,
                            "source_id": item.source_id,
                            "locator": item.locator,
                            "excerpt_hash": item.excerpt_hash,
                            "summary": item.excerpt[:1000],
                        }
                    )
            return {
                "tool_call_count": int(state.get("tool_call_count", 0)) + 1,
                "last_execution_evidence_count": len(items),
                "observations": observations,
                "evidence_refs": references,
                "phase": "CAPABILITY_EXECUTED",
            }

        def observe_progress(state: AgentState):
            coverage = dict(state.get("coverage", {}))
            gap = str(state.get("active_gap") or "")
            count = int(state.get("last_execution_evidence_count", 0))
            no_progress = int(state.get("no_progress_rounds", 0))
            if count:
                coverage[gap] = CoverageStatus.COVERED.value
                no_progress = 0
            else:
                no_progress += 1
            signatures = list(state.get("completed_action_signatures", []))
            signature = state.get("action_signature")
            if signature and signature not in signatures:
                signatures.append(signature)
            return {
                "coverage": coverage,
                "completed_action_signatures": signatures,
                "no_progress_rounds": no_progress,
                "pending_action": None,
                "action_signature": None,
                "phase": "OBSERVED",
            }

        def checkpoint_observation(state: AgentState):
            sequence = self._emit_checkpoint(
                context,
                state,
                sink,
                LoopCheckpointStatus.OBSERVED,
            )
            return {
                "checkpoint_sequence": sequence,
                "status": LoopCheckpointStatus.OBSERVED.value,
                "phase": LoopCheckpointStatus.OBSERVED.value,
            }

        def finish(state: AgentState):
            reason = state.get("stop_reason")
            if not reason:
                reason = (
                    StopReason.COVERAGE_COMPLETE.value
                    if not next_gap(state.get("coverage", {}))
                    else StopReason.NO_PROGRESS.value
                )
            return {
                "stop_reason": reason,
                "phase": "INVESTIGATION_FINISHED",
            }

        def checkpoint_finished(state: AgentState):
            sequence = self._emit_checkpoint(
                context,
                state,
                sink,
                LoopCheckpointStatus.INVESTIGATION_FINISHED,
            )
            return {
                "checkpoint_sequence": sequence,
                "status": LoopCheckpointStatus.INVESTIGATION_FINISHED.value,
                "phase": LoopCheckpointStatus.INVESTIGATION_FINISHED.value,
            }

        builder.add_node("resume", resume)
        builder.add_node("assess", assess_gap)
        builder.add_node("select", select_action)
        builder.add_node("validate", validate_selected_action)
        builder.add_node("checkpoint_action", checkpoint_action)
        builder.add_node("execute", execute_capability)
        builder.add_node("observe", observe_progress)
        builder.add_node("checkpoint_observation", checkpoint_observation)
        builder.add_node("finish", finish)
        builder.add_node("checkpoint_finished", checkpoint_finished)
        builder.add_edge(START, "resume")
        builder.add_conditional_edges(
            "resume",
            route_resume,
            {
                "assess": "assess",
                "execute": "execute",
                "done": END,
            },
        )
        builder.add_conditional_edges(
            "assess",
            route_assessment,
            {"select": "select", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "select",
            route_selection,
            {"validate": "validate", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "validate",
            route_validation,
            {"checkpoint_action": "checkpoint_action", "assess": "assess"},
        )
        builder.add_edge("checkpoint_action", "execute")
        builder.add_edge("execute", "observe")
        builder.add_edge("observe", "checkpoint_observation")
        builder.add_edge("checkpoint_observation", "assess")
        builder.add_edge("finish", "checkpoint_finished")
        builder.add_edge("checkpoint_finished", END)
        return builder.compile()

    def _initial_state(self, context: RunContext) -> AgentState:
        if not context.task_message.strip():
            raise ValueError("task message is required")
        if context.checkpoint:
            checkpoint = self.checkpoint_codec.decode(context.checkpoint)
            if (
                checkpoint.run_id != context.run_id
                or checkpoint.workflow_version
                != (context.workflow_version or "agent-runtime.v1")
                or checkpoint.sequence != context.checkpoint_sequence
            ):
                raise CheckpointError("checkpoint does not match Agent Run")
            snapshot = LoopSnapshot.from_payload(checkpoint.payload)
            restored = dict(snapshot.state)
            try:
                restored_task_version = int(restored.get("task_version", 0))
            except (TypeError, ValueError) as error:
                raise CheckpointError(
                    "loop snapshot task version is invalid"
                ) from error
            if (
                restored.get("run_id") != context.run_id
                or restored.get("task_id") != context.task_id
                or restored_task_version != max(context.task_version, 1)
            ):
                raise CheckpointError("loop snapshot does not match Agent Run context")
            restored["checkpoint_sequence"] = checkpoint.sequence
            artifact_key = restored.get("draft_artifact_key")
            if artifact_key and not restored.get("candidate_markdown"):
                artifact = next(
                    (
                        item
                        for item in context.resume_artifacts
                        if item.artifact_key == artifact_key
                    ),
                    None,
                )
                if artifact is None:
                    raise CheckpointError(
                        f"checkpoint draft artifact is unavailable: {artifact_key}"
                    )
                actual_hash = hashlib.sha256(artifact.content).hexdigest()
                if actual_hash != restored.get("draft_artifact_hash"):
                    raise CheckpointError(
                        "checkpoint draft artifact content hash mismatch"
                    )
                raw_bundle = json.loads(artifact.content.decode("utf-8"))
                markdown = raw_bundle.get("markdown")
                if not isinstance(markdown, str) or not markdown.strip():
                    raise CheckpointError("checkpoint draft artifact is invalid")
                restored["candidate_markdown"] = markdown
            return restored
        elif context.checkpoint_sequence:
            raise CheckpointError("checkpoint sequence exists without payload")
        resume_references = [
            _evidence_reference(item) for item in context.resume_evidence
        ]
        resume_observations = [
            {
                "source_type": item.source_type,
                "source_id": item.source_id,
                "locator": item.locator,
                "excerpt_hash": item.excerpt_hash,
                "summary": item.excerpt[:1000],
            }
            for item in context.resume_evidence
        ]
        immutable_keys = (
            list(context.revision_scope.immutable_unit_keys)
            if context.revision_scope is not None
            else []
        )
        return {
            "schema_version": "agent-graph-state.v1",
            "workflow_version": context.workflow_version or "agent-runtime.v1",
            "run_id": context.run_id,
            "task_id": context.task_id,
            "task_version": max(context.task_version, 1),
            "status": LoopCheckpointStatus.INITIALIZED.value,
            "phase": "INITIALIZED",
            "iteration": 0,
            "tool_call_count": 0,
            "token_usage": 0,
            "replan_count": 0,
            "no_progress_rounds": 0,
            "coverage": {
                key: CoverageStatus.MISSING.value
                for key in dict.fromkeys(self.required_coverage)
            },
            "completed_action_signatures": [],
            "evidence_refs": resume_references,
            "observations": resume_observations,
            "draft_generation": 0,
            "supplement_count": 0,
            "repair_count": 0,
            "immutable_unit_keys": immutable_keys,
            "checkpoint_sequence": context.checkpoint_sequence,
        }

    def _emit_checkpoint(
        self,
        context: RunContext,
        state: AgentState,
        sink: RuntimeEventSink,
        status: LoopCheckpointStatus,
    ) -> int:
        sequence = int(state.get("checkpoint_sequence", 0)) + 1
        serializable = {
            key: value
            for key, value in state.items()
            if key
            not in {
                "last_execution_evidence_count",
                "last_model_attempt_key",
                "draft_bundle",
                "draft_structured",
            }
        }
        if serializable.get("draft_artifact_key"):
            serializable.pop("candidate_markdown", None)
        serializable["checkpoint_sequence"] = sequence
        serializable["status"] = status.value
        serializable["phase"] = status.value
        payload = self.checkpoint_codec.encode(
            workflow_version=context.workflow_version or "agent-runtime.v1",
            run_id=context.run_id,
            task_version=max(context.task_version, 1),
            sequence=sequence,
            payload=LoopSnapshot(status=status, state=serializable).as_payload(),
        )
        sink.checkpoint(sequence, payload)
        return sequence

    def _generate_draft(
        self,
        context: RunContext,
        state: AgentState,
        sink: RuntimeEventSink,
        cancel_event,
    ) -> tuple[str, int]:
        payload = {
            "run_id": context.run_id,
            "task_id": context.task_id,
            "task_message": context.task_message,
            "coverage": state.get("coverage", {}),
            "evidence": state.get("observations", []),
            "stop_reason": state.get("stop_reason"),
        }
        response, _ = self._call_model(
            context,
            sink,
            operation="generate_working_draft",
            attempt_sequence=1,
            payload=payload,
            cancel_event=cancel_event,
        )
        structured = _structured_output(response)
        markdown = structured.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("model response requires a markdown field")
        return markdown, _total_tokens(response)

    def _repair_draft(
        self,
        context: RunContext,
        state: AgentState,
        issues: tuple[dict[str, object], ...],
        sink: RuntimeEventSink,
        cancel_event,
    ) -> tuple[str, int]:
        response, _ = self._call_model(
            context,
            sink,
            operation="repair_working_draft",
            attempt_sequence=int(state.get("repair_count", 0)) + 1,
            payload={
                "run_id": context.run_id,
                "task_id": context.task_id,
                "markdown": state.get("candidate_markdown", ""),
                "quality_issues": list(issues),
                "instruction": "Repair only the listed issues and return markdown.",
            },
            cancel_event=cancel_event,
        )
        markdown = _structured_output(response).get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("repair model response requires a markdown field")
        return markdown, _total_tokens(response)

    def _execute_supplement(
        self,
        context: RunContext,
        gateway,
        query: str,
    ) -> tuple[proto.EvidenceItem, ...]:
        if context.repository_binding_id and context.repository_revision:
            action = ProposedAction(
                tool_id="search_repository",
                tool_schema_version="1",
                arguments={"query": query},
                purpose="Ground an unsupported draft claim",
                target_coverage=("repository_evidence",),
            )
        else:
            action = ProposedAction(
                tool_id="search_prd_catalog",
                tool_schema_version="1",
                arguments={"query": query},
                purpose="Ground an unsupported draft claim",
                target_coverage=("prd_evidence",),
            )
        return self._execute_action(context, gateway, action)

    def _call_model(
        self,
        context: RunContext,
        sink: RuntimeEventSink,
        *,
        operation: str,
        attempt_sequence: int,
        payload: Mapping[str, object],
        cancel_event,
    ) -> tuple[ModelResponse, proto.RecordModelAttemptRequest]:
        request_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = _sha256_text(request_json)
        attempt_key = f"{context.run_id}:{operation}:{attempt_sequence}"
        planned = proto.RecordModelAttemptRequest(
            attempt_key=attempt_key,
            operation=operation,
            prompt_version=f"agent-runtime.{operation}.v2",
            provider="deepseek",
            request_hash=request_hash,
            status="PLANNED",
            response_metadata_json="{}",
            token_usage_json="{}",
        )
        if context.plan_model_attempt is not None:
            context.plan_model_attempt(planned)
        sink.model_attempt(planned)
        _raise_if_cancelled(cancel_event)
        try:
            response = self.model.complete(
                (
                    "You are the PRD Agent runtime. Return one JSON object. "
                    "Use only supplied observations for repository claims and "
                    "never include credentials or hidden reasoning."
                ),
                request_json,
            )
        except ModelApiError as error:
            sink.model_attempt(
                proto.RecordModelAttemptRequest(
                    attempt_key=attempt_key,
                    operation=operation,
                    prompt_version=f"agent-runtime.{operation}.v2",
                    provider="deepseek",
                    request_hash=request_hash,
                    status="FAILED",
                    response_metadata_json="{}",
                    token_usage_json="{}",
                    error_category=error.code,
                )
            )
            raise
        _raise_if_cancelled(cancel_event)
        attempt = proto.RecordModelAttemptRequest(
            attempt_key=attempt_key,
            operation=operation,
            prompt_version=f"agent-runtime.{operation}.v2",
            provider="deepseek",
            request_hash=request_hash,
            status="SUCCEEDED",
            response_metadata_json=json.dumps(
                {
                    "model_id": response.model_id,
                    "finish_reason": response.finish_reason,
                    "provider_request_id": response.provider_request_id,
                    "latency_ms": response.latency_ms,
                    "output_hash": _sha256_text(response.output),
                },
                sort_keys=True,
            ),
            token_usage_json=json.dumps(response.token_usage, sort_keys=True),
        )
        sink.model_attempt(attempt)
        return response, attempt

    @staticmethod
    def _execute_action(
        context: RunContext,
        gateway,
        action: ProposedAction,
    ) -> tuple[proto.EvidenceItem, ...]:
        query = str(action.arguments["query"]).strip()
        if action.tool_id == "search_repository":
            if not context.repository_binding_id or not context.repository_revision:
                raise ValueError("Agent Run lacks repository capability context")
            hits = gateway.search_repository(
                binding_id=context.repository_binding_id,
                revision=context.repository_revision,
                query=query,
                limit=10,
            )
            return repository_hits_to_evidence(
                context.repository_binding_id,
                context.repository_revision,
                hits,
                limit=100,
            )
        catalog_hits = gateway.search_prd_catalog(query=query, limit=10)
        locator_ids = [item.locator_id for item in catalog_hits if item.locator_id]
        if not locator_ids:
            return ()
        sections = gateway.fetch_prd_sections(locator_ids)
        items = []
        for section in sections:
            excerpt = section.markdown[:8000]
            items.append(
                proto.EvidenceItem(
                    source_type="prd",
                    source_id=section.source_revision,
                    locator=section.locator_id,
                    excerpt_hash=_sha256_text(excerpt),
                    excerpt=excerpt,
                )
            )
            if len(items) >= 100:
                break
        return tuple(items)


def _structured_output(response: ModelResponse) -> dict:
    try:
        structured = json.loads(response.output)
    except json.JSONDecodeError as error:
        raise ValueError("model returned invalid JSON") from error
    if not isinstance(structured, dict):
        raise ValueError("model returned a non-object JSON value")
    return structured


def _total_tokens(response: ModelResponse) -> int:
    usage = response.token_usage
    value = usage.get("total_tokens") or usage.get("total")
    if isinstance(value, int):
        return value
    return sum(item for item in usage.values() if isinstance(item, int))


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_reference(item: proto.EvidenceItem) -> str:
    return _sha256_text(
        "\x00".join(
            (
                item.source_type,
                item.source_id,
                item.locator,
                item.excerpt_hash,
            )
        )
    )


def _raise_if_cancelled(cancel_event) -> None:
    if cancel_event is None:
        return
    method = getattr(cancel_event, "raise_if_cancelled", None)
    if callable(method):
        method()
    elif cancel_event.is_set():
        raise RuntimeError("agent run was cancelled")


def _is_advanced_status(value: object) -> bool:
    return str(value) in {
        LoopCheckpointStatus.DRAFTED.value,
        LoopCheckpointStatus.GROUNDING_SUPPLEMENT_REQUIRED.value,
        LoopCheckpointStatus.GROUNDED.value,
        LoopCheckpointStatus.GROUNDING_PARTIAL.value,
        LoopCheckpointStatus.QUALITY_REPAIR_REQUIRED.value,
        LoopCheckpointStatus.QUALITY_PASSED.value,
        LoopCheckpointStatus.QUALITY_NEEDS_HUMAN.value,
        LoopCheckpointStatus.CONFIRMATION_UNITS_BUILT.value,
    }
