from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol
import uuid

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.hashing import sha256_json
from prd_agent.workflow.nodes import invoke_validated

from .models import InformationNeed, ProposedAction, Requiredness
from .policies import COVERAGE_TEMPLATES, RequirednessPolicy


class PlannedNeedDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=1000)
    suggested_requiredness: Requiredness
    need_kind: str = Field(min_length=1)
    source_types: tuple[str, ...] = ()
    suggested_coverage: tuple[str, ...] = ()
    fallback: str = Field(min_length=1)
    public_reason: str = Field(min_length=1)


class InformationNeedPlanner:
    planner_version = "information-need.v1"

    def __init__(self, model, policy: RequirednessPolicy | None = None) -> None:
        self.model = model
        self.policy = policy or RequirednessPolicy()

    def plan(
        self,
        context: Mapping[str, Any],
        *,
        trigger_stage: str,
        task_id: str | None = None,
        run_id: str | None = None,
        unit_id: str | None = None,
    ) -> InformationNeed:
        draft = invoke_validated(
            self.model,
            "plan_information_need",
            dict(context),
            PlannedNeedDraft.model_validate,
        )
        context_text = sha256_json(context) + " " + " ".join(
            str(value) for value in context.values()
        )
        requiredness = self.policy.decide(draft.suggested_requiredness, context_text)
        coverage = tuple(dict.fromkeys(draft.suggested_coverage))
        if requiredness == Requiredness.NONE:
            coverage = ()
            source_types = ()
        else:
            coverage = coverage or COVERAGE_TEMPLATES.get(draft.need_kind, ())
            source_types = draft.source_types or ("CODE",)
        return InformationNeed(
            information_need_id=f"need-{uuid.uuid4().hex}",
            task_id=task_id,
            run_id=run_id,
            unit_id=unit_id,
            trigger_stage=trigger_stage,
            question=draft.question,
            requiredness=requiredness,
            source_types=source_types,
            required_coverage=coverage,
            fallback=draft.fallback,
            planner_version=self.planner_version,
            context_hash=sha256_json(context),
        )


class ActionSelector(Protocol):
    def select(
        self,
        payload: Mapping[str, Any],
        *,
        repair: bool = False,
        replan: bool = False,
    ) -> ProposedAction:
        ...


class ModelActionSelector:
    def __init__(self, model) -> None:
        self.model = model
        self.last_token_usage = 0
        self.last_model_result = None

    def select(
        self,
        payload: Mapping[str, Any],
        *,
        repair: bool = False,
        replan: bool = False,
    ) -> ProposedAction:
        operation = "replan_investigation_action" if replan else "select_investigation_action"
        first_error = None
        for should_repair in (repair, True):
            result = self.model.complete(operation, payload, repair=should_repair)
            self.last_model_result = result
            usage = dict(result.token_usage)
            self.last_token_usage += int(
                usage.get("total_tokens")
                or usage.get("total")
                or sum(value for value in usage.values() if isinstance(value, int))
            )
            try:
                return ProposedAction.model_validate(result.structured_output)
            except (TypeError, ValueError) as exc:
                first_error = exc
        raise ValueError(f"invalid investigation action after one repair: {first_error}")


class ScriptedActionSelector:
    """Deterministic selector for tests and offline evaluation."""

    def __init__(self, actions: list[ProposedAction | Mapping[str, Any]]) -> None:
        if not actions:
            raise ValueError("scripted selector requires at least one action")
        self.actions = [ProposedAction.model_validate(item) for item in actions]
        self.index = 0
        self.last_token_usage = 0

    def select(self, payload, *, repair: bool = False, replan: bool = False):
        index = min(self.index, len(self.actions) - 1)
        self.index += 1
        return self.actions[index]
