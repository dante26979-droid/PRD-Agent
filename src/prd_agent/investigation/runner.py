from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import uuid

from prd_agent.hashing import sha256_json
from prd_agent.model_api.errors import ModelApiError
from prd_agent.tools.models import ToolAction, ToolStatus
from prd_agent.tools.registry import action_signature

from .models import (
    Investigation,
    InvestigationResult,
    InvestigationStatus,
    InvestigationStep,
    StopReason,
)
from .policies import CoveragePolicy, RoutePolicy, progress_fingerprint


class InvestigationRunner:
    def __init__(
        self,
        evidence_service,
        store,
        selector,
        *,
        coverage_policy: CoveragePolicy | None = None,
        route_policy: RoutePolicy | None = None,
    ) -> None:
        self.evidence_service = evidence_service
        self.store = store
        self.selector = selector
        self.coverage_policy = coverage_policy or CoveragePolicy()
        self.route_policy = route_policy or RoutePolicy()

    def run(self, investigation_id: str, *, actor_id: str = "local") -> InvestigationResult:
        investigation = self.store.get_investigation(investigation_id)
        if investigation.status == InvestigationStatus.PLANNED:
            investigation = self._save(
                investigation,
                status=InvestigationStatus.RUNNING,
            )
            self._step(investigation, "START", "SUCCEEDED", "调查已开始")
        elif investigation.status != InvestigationStatus.RUNNING:
            return self._result(investigation)

        while investigation.status == InvestigationStatus.RUNNING:
            if self.coverage_policy.complete(investigation.coverage):
                investigation = self._finish(investigation, StopReason.COVERAGE_COMPLETE)
                break
            pre_stop = self.route_policy.pre_action_stop(investigation)
            if pre_stop:
                investigation = self._finish(investigation, pre_stop)
                break

            gap = self.coverage_policy.next_gap(investigation.coverage)
            investigation = self._save(
                investigation,
                iteration_count=investigation.iteration_count + 1,
            )
            self._step(
                investigation,
                "ASSESS_GAPS",
                "SUCCEEDED",
                f"当前优先调查缺口：{gap or '无'}",
                {"gap": gap},
            )
            replan = investigation.no_progress_rounds > 0 and (
                investigation.replan_count < investigation.budget.max_replans
            )
            if replan:
                investigation = self._save(
                    investigation, replan_count=investigation.replan_count + 1
                )
                self._step(investigation, "REPLAN", "SUCCEEDED", "已执行一次调查重规划")

            payload = self._selector_payload(investigation, gap)
            try:
                proposal = self.selector.select(payload, replan=replan)
                model_result = getattr(
                    self.selector,
                    "last_model_result",
                    None,
                )
                if model_result is not None:
                    self._step(
                        investigation,
                        "CALL_MODEL",
                        "SUCCEEDED",
                        "模型已返回结构化调查动作",
                        {
                            "model_id": model_result.model_id,
                            "prompt_version": model_result.prompt_version,
                            "raw_output_hash": model_result.raw_output_hash,
                            "token_usage": dict(
                                model_result.token_usage
                            ),
                            "finish_reason": model_result.finish_reason,
                        },
                    )
                selection_tokens = int(getattr(self.selector, "last_token_usage", 0))
                if selection_tokens:
                    investigation = self._save(
                        investigation,
                        token_usage=investigation.token_usage + selection_tokens,
                    )
                    self.selector.last_token_usage = 0
                if investigation.token_usage >= investigation.budget.token_budget:
                    investigation = self._finish(
                        investigation, StopReason.TOKEN_BUDGET_EXHAUSTED
                    )
                    continue
                active_statuses = {"MISSING", "PARTIAL", "CONFLICTING"}
                if not proposal.target_coverage or not any(
                    key in investigation.coverage
                    and investigation.coverage[key].status.value in active_statuses
                    for key in proposal.target_coverage
                ):
                    raise ValueError("action does not target an active coverage gap")
                action = ToolAction(
                    tool_id=proposal.tool_id,
                    tool_schema_version=proposal.tool_schema_version,
                    repository_id=investigation.repository_id,
                    resolved_commit_sha=investigation.resolved_commit_sha,
                    arguments=proposal.arguments,
                    purpose=proposal.purpose,
                )
                self.evidence_service.registry.validate(action)
            except ModelApiError as exc:
                self._step(
                    investigation,
                    "CALL_MODEL",
                    "FAILED",
                    "模型服务未能返回调查动作",
                    {
                        "error_code": exc.code,
                        "retryable": exc.retryable,
                    },
                )
                investigation = self._finish(
                    investigation,
                    StopReason.UNRECOVERABLE_ERROR,
                )
                raise
            except Exception:
                failed_selection_tokens = int(
                    getattr(self.selector, "last_token_usage", 0)
                )
                if failed_selection_tokens:
                    investigation = self._save(
                        investigation,
                        token_usage=investigation.token_usage + failed_selection_tokens,
                    )
                    self.selector.last_token_usage = 0
                self._step(
                    investigation,
                    "VALIDATE_ACTION",
                    "FAILED",
                    "调查动作未通过结构或工具策略校验",
                )
                investigation = self._record_no_progress(investigation)
                if investigation.token_usage >= investigation.budget.token_budget:
                    investigation = self._finish(
                        investigation, StopReason.TOKEN_BUDGET_EXHAUSTED
                    )
                elif investigation.no_progress_rounds >= investigation.budget.no_progress_limit:
                    investigation = self._finish(investigation, StopReason.NO_PROGRESS)
                continue

            signature = action_signature(action)
            if signature in investigation.completed_action_signatures:
                self._step(
                    investigation,
                    "VALIDATE_ACTION",
                    "BLOCKED",
                    "重复调查动作已被拒绝",
                    {"action_signature": signature},
                )
                investigation = self._record_no_progress(investigation)
                if investigation.no_progress_rounds >= investigation.budget.no_progress_limit:
                    investigation = self._finish(investigation, StopReason.NO_PROGRESS)
                continue

            self._step(
                investigation,
                "SELECT_NEXT_ACTION",
                "SUCCEEDED",
                proposal.purpose,
                {
                    "tool_id": proposal.tool_id,
                    "target_coverage": list(proposal.target_coverage),
                    "action_signature": signature,
                },
            )
            before = progress_fingerprint(investigation)
            idempotency_key = (
                f"investigation:{investigation.investigation_id}:"
                f"action:{signature}:attempt:1"
            )
            replay = self.evidence_service.store.get_replay(actor_id, idempotency_key)
            if replay is None:
                investigation = self._save(
                    investigation,
                    tool_call_count=investigation.tool_call_count + 1,
                )
            try:
                execution = self.evidence_service.execute(
                    action,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    task_id=investigation.task_id,
                    run_id=investigation.run_id,
                    unit_id=investigation.unit_id,
                    investigation_id=investigation.investigation_id,
                )
            except Exception:
                self._step(
                    investigation,
                    "EXECUTE_TOOL",
                    "FAILED",
                    "调查工具调用未能安全完成",
                )
                investigation = self._record_no_progress(investigation)
                if investigation.no_progress_rounds >= investigation.budget.no_progress_limit:
                    investigation = self._finish(investigation, StopReason.NO_PROGRESS)
                continue

            updates = self._execution_updates(investigation, proposal, execution, signature)
            investigation = self._save(investigation, **updates)
            after = progress_fingerprint(investigation)
            has_progress = bool(after - before)
            investigation = self._save(
                investigation,
                no_progress_rounds=0
                if has_progress
                else investigation.no_progress_rounds + 1,
            )
            self._step(
                investigation,
                "EXECUTE_TOOL",
                execution.tool_result.status.value,
                execution.tool_result.public_summary,
                {
                    "tool_call_id": execution.tool_call.tool_call_id,
                    "has_progress": has_progress,
                },
            )
            if self.coverage_policy.complete(investigation.coverage):
                investigation = self._finish(investigation, StopReason.COVERAGE_COMPLETE)
            elif investigation.no_progress_rounds >= investigation.budget.no_progress_limit:
                investigation = self._finish(investigation, StopReason.NO_PROGRESS)

        return self._result(investigation)

    def cancel(self, investigation_id: str) -> InvestigationResult:
        investigation = self.store.get_investigation(investigation_id)
        if investigation.status in {InvestigationStatus.PLANNED, InvestigationStatus.RUNNING}:
            investigation = self._finish(investigation, StopReason.USER_STOPPED)
        return self._result(investigation)

    def _execution_updates(self, investigation, proposal, execution, signature):
        coverage = self.coverage_policy.update(investigation, proposal, execution)
        return {
            "coverage": coverage,
            "completed_action_signatures": frozenset(
                (*investigation.completed_action_signatures, signature)
            )
            if execution.tool_result.status
            in {ToolStatus.SUCCEEDED, ToolStatus.PARTIAL, ToolStatus.EMPTY}
            else investigation.completed_action_signatures,
            "evidence_ids": _merge(
                investigation.evidence_ids,
                tuple(item.evidence_id for item in execution.bundle.evidence),
            ),
            "fact_ids": _merge(
                investigation.fact_ids,
                tuple(item.fact_id for item in execution.bundle.facts),
            ),
            "unknown_ids": _merge(
                investigation.unknown_ids,
                tuple(item.unknown_id for item in execution.bundle.unknowns),
            ),
            "conflict_ids": _merge(
                investigation.conflict_ids,
                tuple(item.conflict_id for item in execution.bundle.conflicts),
            ),
        }

    def _record_no_progress(self, investigation):
        return self._save(
            investigation, no_progress_rounds=investigation.no_progress_rounds + 1
        )

    def _finish(self, investigation: Investigation, reason: StopReason):
        status = self.route_policy.terminal_status(investigation, reason)
        finished = self._save(investigation, status=status, stop_reason=reason)
        self._step(
            finished,
            "FINALIZE",
            "SUCCEEDED",
            f"调查结束：{reason.value}",
            {"status": status.value, "stop_reason": reason.value},
        )
        return finished

    def _save(self, investigation: Investigation, **updates) -> Investigation:
        old_version = investigation.version
        updated = investigation.model_copy(
            update={
                **updates,
                "version": old_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.store.save_investigation(updated, expected_version=old_version)
        return updated

    def _step(
        self,
        investigation,
        step_type: str,
        status: str,
        summary: str,
        output: dict[str, Any] | None = None,
    ) -> None:
        sequence = len(self.store.steps(investigation.investigation_id)) + 1
        payload = output or {}
        self.store.append_step(
            InvestigationStep(
                step_id=f"investigation-step-{uuid.uuid4().hex}",
                investigation_id=investigation.investigation_id,
                sequence=sequence,
                iteration=investigation.iteration_count,
                step_type=step_type,
                status=status,
                public_summary=summary,
                input_hash=sha256_json(payload),
                output=payload,
            )
        )

    def _selector_payload(self, investigation, gap):
        need = self.store.get_need(investigation.information_need_id)
        prior_steps = self.store.steps(investigation.investigation_id)
        return {
            "investigation_id": investigation.investigation_id,
            "information_need_id": investigation.information_need_id,
            "question": need.question,
            "requiredness": need.requiredness.value,
            "repository_id": investigation.repository_id,
            "resolved_commit_sha": investigation.resolved_commit_sha,
            "gap": gap,
            "coverage": {
                key: item.status.value for key, item in investigation.coverage.items()
            },
            "completed_action_signatures": sorted(
                investigation.completed_action_signatures
            ),
            "available_tools": list(self.evidence_service.registry.describe()),
            "prior_public_steps": [
                {
                    "iteration": step.iteration,
                    "step_type": step.step_type,
                    "status": step.status,
                    "summary": step.public_summary,
                }
                for step in prior_steps[-10:]
            ],
            "prior_tool_results": self._prior_tool_results(prior_steps),
            "remaining_budget": {
                "iterations": investigation.budget.max_iterations
                - investigation.iteration_count,
                "tool_calls": investigation.budget.max_tool_calls
                - investigation.tool_call_count,
                "replans": investigation.budget.max_replans
                - investigation.replan_count,
                "tokens": investigation.budget.token_budget - investigation.token_usage,
            },
        }

    def _prior_tool_results(self, prior_steps):
        values = []
        for step in prior_steps:
            if step.step_type != "EXECUTE_TOOL":
                continue
            tool_call_id = step.output.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                continue
            try:
                execution = self.evidence_service.store.get_execution(
                    tool_call_id
                )
            except (KeyError, TypeError, ValueError):
                continue
            values.append(
                {
                    "iteration": step.iteration,
                    "tool_id": execution.tool_call.tool_id,
                    "status": execution.tool_result.status.value,
                    "public_summary": execution.tool_result.public_summary,
                    "evidence_ids": [
                        item.evidence_id
                        for item in execution.bundle.evidence
                    ],
                    "fact_ids": [
                        item.fact_id for item in execution.bundle.facts
                    ],
                    "unknown_ids": [
                        item.unknown_id
                        for item in execution.bundle.unknowns
                    ],
                    "conflict_ids": [
                        item.conflict_id
                        for item in execution.bundle.conflicts
                    ],
                }
            )
        return values[-5:]

    def _result(self, investigation: Investigation) -> InvestigationResult:
        reason = investigation.stop_reason or StopReason.UNRECOVERABLE_ERROR
        risks = tuple(
            f"{key}: {item.status.value}"
            for key, item in investigation.coverage.items()
            if item.status.value not in {"COVERED", "NOT_REQUIRED"}
        )
        return InvestigationResult(
            investigation_id=investigation.investigation_id,
            status=investigation.status,
            stop_reason=reason,
            coverage=investigation.coverage,
            evidence_ids=investigation.evidence_ids,
            fact_ids=investigation.fact_ids,
            unknown_ids=investigation.unknown_ids,
            conflict_ids=investigation.conflict_ids,
            public_summary=f"调查以 {investigation.status.value} 结束：{reason.value}",
            risk_summary=risks,
        )


def _merge(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*left, *right)))
