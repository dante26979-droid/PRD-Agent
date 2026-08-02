from __future__ import annotations

from collections.abc import Callable

from agent.knowledge import EvidenceKnowledgeModule, KnowledgeBuildRequest

from .models import (
    ActionRecord,
    ActionSelectionContext,
    InvestigationMode,
    InvestigationRequest,
    InvestigationResult,
    InvestigationStatus,
    ProposedAction,
    StopReason,
)
from .policies import (
    DuplicateActionError,
    action_signature,
    coverage_complete,
    next_gap,
    validate_action,
)


class InvestigationRunner:
    """One bounded implementation for initial, replan and supplement rounds."""

    def __init__(
        self,
        *,
        select_action: Callable[[ActionSelectionContext], ProposedAction],
        execute_action: Callable[[ProposedAction], tuple[object, ...]],
        knowledge_module: EvidenceKnowledgeModule,
        on_action_validated: Callable[[ProposedAction, str], None] | None = None,
        on_observed: Callable[[InvestigationResult], None] | None = None,
    ) -> None:
        self._select_action = select_action
        self._execute_action = execute_action
        self._knowledge = knowledge_module
        self._on_action_validated = on_action_validated
        self._on_observed = on_observed

    def run(self, request: InvestigationRequest) -> InvestigationResult:
        coverage = dict(request.coverage)
        knowledge = request.prior_knowledge
        history = list(request.prior_action_history)
        attempted = [item.signature for item in history]
        history_offset = (
            len(history) if request.mode is InvestigationMode.SUPPLEMENT else 0
        )
        replans = (
            0
            if request.mode is InvestigationMode.SUPPLEMENT
            else sum(item.mode is InvestigationMode.REPLAN for item in history)
        )
        no_progress = 0
        next_mode = request.mode
        no_progress_reason: str | None = None
        pending_action = request.pending_action

        while True:
            if coverage_complete(coverage):
                return self._result(
                    InvestigationStatus.COMPLETE,
                    StopReason.COVERAGE_COMPLETE,
                    coverage,
                    knowledge,
                    history,
                    replans,
                    no_progress,
                )
            current_rounds = len(history) - history_offset
            if current_rounds >= request.budget.max_iterations:
                return self._result(
                    _partial_status(knowledge),
                    StopReason.MAX_ITERATIONS_REACHED,
                    coverage,
                    knowledge,
                    history,
                    replans,
                    no_progress,
                )
            if current_rounds >= request.budget.max_tool_calls:
                return self._result(
                    _partial_status(knowledge),
                    StopReason.TOOL_BUDGET_EXHAUSTED,
                    coverage,
                    knowledge,
                    history,
                    replans,
                    no_progress,
                )
            gap = next_gap(coverage)
            if gap is None:
                return self._result(
                    InvestigationStatus.COMPLETE,
                    StopReason.COVERAGE_COMPLETE,
                    coverage,
                    knowledge,
                    history,
                    replans,
                    no_progress,
                )
            resumed_action = pending_action is not None
            if resumed_action:
                action = pending_action
                signature = validate_action(
                    action,
                    active_gap=gap,
                    completed_signatures=tuple(attempted),
                )
                if request.pending_signature and signature != request.pending_signature:
                    raise ValueError("validated Action Signature mismatch")
                pending_action = None
            else:
                selection = ActionSelectionContext(
                    mode=next_mode,
                    active_gap=gap,
                    coverage=dict(coverage),
                    action_history=tuple(history),
                    no_progress_reason=no_progress_reason,
                    remaining_iterations=request.budget.max_iterations - current_rounds,
                    remaining_tool_calls=request.budget.max_tool_calls - current_rounds,
                    remaining_replans=request.budget.max_replans - replans,
                )
                action = self._select_action(selection)
                try:
                    signature = validate_action(
                        action,
                        active_gap=gap,
                        completed_signatures=tuple(attempted),
                    )
                except DuplicateActionError:
                    return self._result(
                        _partial_status(knowledge),
                        StopReason.REPLAN_EXHAUSTED,
                        coverage,
                        knowledge,
                        history,
                        replans,
                        no_progress + 1,
                    )
            if not resumed_action and self._on_action_validated is not None:
                self._on_action_validated(action, signature)
            items = self._execute_action(action)
            before_facts = {
                item.fact_id for item in getattr(knowledge, "facts", ())
            }
            build = self._knowledge.build(
                KnowledgeBuildRequest(
                    run_id=request.run_id,
                    task_id=request.task_id,
                    need_plan_id=request.need_plan_id,
                    need_context_hash=request.need_context_hash,
                    action=action,
                    evidence_items=tuple(items),
                    source_authorities=tuple(request.source_authorities),
                    coverage=coverage,
                    prior_bundle=knowledge,
                    outcome_kind="HIT" if items else "EMPTY",
                )
            )
            after_facts = {
                item.fact_id
                for item in build.bundle.facts
                if item.verification_status.value == "SUPPORTED"
            }
            new_fact_ids = tuple(sorted(after_facts - before_facts))
            history.append(
                ActionRecord(
                    round_index=len(history) + 1,
                    mode=next_mode,
                    signature=signature,
                    action=action,
                    new_fact_ids=new_fact_ids,
                    progress_before=getattr(knowledge, "progress_fingerprint", None),
                    progress_after=build.bundle.progress_fingerprint,
                )
            )
            attempted.append(signature)
            coverage = build.coverage
            knowledge = build.bundle
            if new_fact_ids:
                no_progress = 0
                next_mode = request.mode
                no_progress_reason = None
            else:
                no_progress += 1
                no_progress_reason = (
                    "EMPTY_RESULT" if not items else "ONLY_RELATED_EVIDENCE"
                )
                if replans >= request.budget.max_replans:
                    result = self._result(
                        _partial_status(knowledge),
                        StopReason.NO_PROGRESS,
                        coverage,
                        knowledge,
                        history,
                        replans,
                        no_progress,
                    )
                    if self._on_observed is not None:
                        self._on_observed(result)
                    return result
                replans += 1
                next_mode = InvestigationMode.REPLAN
            if self._on_observed is not None:
                self._on_observed(
                    self._result(
                        _partial_status(knowledge),
                        StopReason.PARTIAL_COVERAGE,
                        coverage,
                        knowledge,
                        history,
                        replans,
                        no_progress,
                    )
                )

    @staticmethod
    def _result(
        status: InvestigationStatus,
        stop_reason: StopReason,
        coverage: dict[str, str],
        knowledge: object | None,
        history: list[ActionRecord],
        replans: int,
        no_progress: int,
    ) -> InvestigationResult:
        return InvestigationResult(
            status=status,
            stop_reason=stop_reason,
            coverage=dict(coverage),
            knowledge=knowledge,
            action_history=tuple(history),
            replan_count=replans,
            no_progress_rounds=no_progress,
        )


def _partial_status(knowledge: object | None) -> InvestigationStatus:
    if knowledge is None:
        return InvestigationStatus.EMPTY
    if getattr(knowledge, "facts", ()):
        return InvestigationStatus.PARTIAL
    return InvestigationStatus.EMPTY
