from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import uuid

from prd_agent.domain.commands import (
    ApproveRevisionPlan,
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    ReopenPrd,
    ReplyToTask,
    StartTask,
)
from prd_agent.domain.entities import (
    AgentRun,
    ConfirmationUnit,
    IdempotencyRecord,
    OutlineNode,
    OutlineVersion,
    PrdDocumentVersion,
    PrdSectionVersion,
    RequirementBrief,
    RequirementBriefVersion,
    Task,
    TaskMessage,
    WorkflowSnapshot,
)
from prd_agent.domain.enums import (
    Complexity,
    OutlineStatus,
    RunStatus,
    DocumentStatus,
    TaskStatus,
    UnitStatus,
)
from prd_agent.domain.errors import (
    IdempotencyConflict,
    InvalidCommand,
    InvalidTransition,
    ModelOutputError,
    NotFound,
    VersionConflict,
)
from prd_agent.hashing import sha256_json
from prd_agent.grounding.models import (
    ClaimCriticality,
    ClaimKind,
    GroundingRequest,
    PrdClaim,
)
from prd_agent.policies.outline_policy import OutlinePolicy
from prd_agent.policies.task_policy import TaskTransitionPolicy
from prd_agent.policies.unit_policy import UnitPolicy
from prd_agent.rendering.markdown import render_markdown
from prd_agent.rendering.evidence_appendix import render_evidence_appendix
from prd_agent.workflow.model import WorkflowModel
from prd_agent.workflow.context_builder import build_confirmed_context
from prd_agent.workflow.nodes import (
    extract_requirement_brief,
    invoke_validated,
    validate_outline,
    validate_unit_draft_for_nodes,
)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class WorkflowService:
    def __init__(
        self,
        repository,
        model: WorkflowModel,
        unit_context_provider=None,
        grounding_service=None,
        quality_service=None,
    ) -> None:
        self.repository = repository
        self.model = model
        self.unit_context_provider = unit_context_provider
        self.grounding_service = grounding_service
        self.quality_service = quality_service
        self.task_policy = TaskTransitionPolicy()
        self.outline_policy = OutlinePolicy()
        self.unit_policy = UnitPolicy()

    def start_task(self, command: StartTask) -> WorkflowSnapshot:
        if not command.idempotency_key.strip():
            raise InvalidCommand("idempotency key cannot be empty")
        guard = getattr(self.repository, "idempotency_lock", None)
        if guard is None:
            return self._start_task_locked(command)
        with guard(command.actor_id, command.idempotency_key):
            return self._start_task_locked(command)

    def _start_task_locked(self, command: StartTask) -> WorkflowSnapshot:
        message = command.message.strip()
        if not message:
            raise InvalidCommand("start message cannot be empty")
        if not command.idempotency_key.strip():
            raise InvalidCommand("idempotency key cannot be empty")
        input_hash = sha256_json({"command": "StartTask", "message": message})
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict("idempotency key was already used with different input")
            return self._owned_snapshot(replay.task_id, command.actor_id)

        task_id = _id("task")
        run_id = _id("run")
        thread_id = _id("thread")
        now = datetime.now(timezone.utc)
        try:
            brief = extract_requirement_brief(self.model, message)
        except ModelOutputError as exc:
            task = Task(
                task_id=task_id,
                title=message[:120],
                owner_id=command.actor_id,
            )
            task.status = self.task_policy.transition(
                task.status,
                TaskStatus.CLARIFYING,
                current_version=task.version,
                expected_version=task.version,
            )
            task.version += 1
            task.status = self.task_policy.transition(
                task.status,
                TaskStatus.FAILED,
                current_version=task.version,
                expected_version=task.version,
            )
            task.version += 1
            task.updated_at = now
            run = AgentRun(
                run_id=run_id,
                task_id=task_id,
                thread_id=thread_id,
                status=RunStatus.FAILED,
                input_hash=input_hash,
                idempotency_key=command.idempotency_key,
                started_at=now,
                ended_at=now,
                error=str(exc),
            )
            self.repository.create_task_bundle(
                task,
                TaskMessage(_id("message"), task_id, command.actor_id, message),
                RequirementBriefVersion(
                    task_id,
                    1,
                    RequirementBrief(
                        problem=message,
                        open_questions=("需求结构化失败，需要用户重试或补充信息",),
                    ),
                    confirmed=False,
                ),
                run,
                IdempotencyRecord(
                    command.actor_id,
                    command.idempotency_key,
                    input_hash,
                    task_id,
                    run_id,
                ),
            )
            self._event(task_id, "RunFailed", {"node": "EXTRACT_REQUIREMENT_BRIEF"})
            self._checkpoint(task, run, "EXTRACT_REQUIREMENT_BRIEF_FAILED")
            raise
        task = Task(
            task_id=task_id,
            title=brief.problem[:120],
            owner_id=command.actor_id,
        )
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.CLARIFYING,
            current_version=task.version,
            expected_version=task.version,
        )
        task.version += 1
        task.updated_at = now
        run = AgentRun(
            run_id=run_id,
            task_id=task_id,
            thread_id=thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=now,
        )
        self.repository.create_task_bundle(
            task,
            TaskMessage(_id("message"), task_id, command.actor_id, message),
            RequirementBriefVersion(task_id, 1, brief),
            run,
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task_id,
                run_id,
            ),
        )
        self._event(task_id, "TaskStarted", {"run_id": run_id})

        if brief.needs_clarification:
            run.status = RunStatus.WAITING_USER
            self.repository.save_run(run)
            self._event(
                task_id,
                "ClarificationRequested",
                {"question_count": len(brief.open_questions)},
            )
            self._checkpoint(task, run, "WAIT_USER_CLARIFICATION")
            return self.repository.snapshot(task_id)

        return self._generate_outline(task, run, brief)

    def reply_to_task(self, command: ReplyToTask) -> WorkflowSnapshot:
        message = command.message.strip()
        if not message:
            raise InvalidCommand("reply message cannot be empty")
        input_hash = sha256_json(
            {
                "command": "ReplyToTask",
                "task_id": command.task_id,
                "message": message,
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict("idempotency key was already used with different input")
            return self._owned_snapshot(replay.task_id, command.actor_id)

        task = self._owned_task(command.task_id, command.actor_id)
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status not in {TaskStatus.CLARIFYING, TaskStatus.OUTLINE_REVIEW}:
            raise InvalidTransition("reply is only accepted while clarifying or reviewing outline")
        previous = self.repository.snapshot(task.task_id)
        brief = extract_requirement_brief(self.model, message, previous.current_brief.brief)
        if task.status == TaskStatus.OUTLINE_REVIEW:
            task.status = self.task_policy.transition(
                task.status,
                TaskStatus.CLARIFYING,
                current_version=task.version,
                expected_version=command.expected_task_version,
            )
            task.version += 1
            task.updated_at = datetime.now(timezone.utc)
            old_outline = previous.current_outline
            if old_outline:
                old_outline.status = OutlineStatus.SUPERSEDED
                self.repository.save_outline(old_outline)
            self.repository.save_task(task)
        old_run = previous.run
        old_run.status = RunStatus.SUCCEEDED
        old_run.ended_at = datetime.now(timezone.utc)
        self.repository.save_run(old_run)
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=old_run.thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.save_run(run)
        self.repository.save_message(
            TaskMessage(_id("message"), task.task_id, command.actor_id, message)
        )
        self.repository.save_brief(
            RequirementBriefVersion(task.task_id, len(previous.briefs) + 1, brief)
        )
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        if brief.needs_clarification:
            run.status = RunStatus.WAITING_USER
            self.repository.save_run(run)
            self._event(
                task.task_id,
                "ClarificationRequested",
                {"question_count": len(brief.open_questions)},
            )
            self._checkpoint(task, run, "WAIT_USER_CLARIFICATION")
            return self.repository.snapshot(task.task_id)
        return self._generate_outline(task, run, brief)

    def _generate_outline(self, task: Task, run: AgentRun, brief) -> WorkflowSnapshot:
        outline_draft = invoke_validated(
            self.model,
            "generate_outline",
            {"requirement_brief": brief.as_dict()},
            validate_outline,
        )
        outline_id = _id("outline")
        prior_outlines = self.repository.snapshot(task.task_id).outlines
        outline_version = max((item.version for item in prior_outlines), default=0) + 1
        node_ids_by_key: dict[str, str] = {}
        nodes: list[OutlineNode] = []
        for sequence, node_value in enumerate(outline_draft["nodes"], start=1):
            node_id = _id("node")
            node_ids_by_key[str(node_value["key"])] = node_id
            nodes.append(
                OutlineNode(
                    node_id=node_id,
                    sequence=sequence,
                    title=str(node_value["title"]),
                    purpose=str(node_value["purpose"]),
                    complexity=Complexity(str(node_value["complexity"])),
                    required_information=tuple(
                        node_value.get("required_information", [])
                    ),
                    parent_id=(
                        node_ids_by_key[str(node_value["parent_key"])]
                        if node_value.get("parent_key")
                        else None
                    ),
                    level=int(node_value.get("level", 1)),
                    stable_key=str(node_value["key"]),
                )
            )
        unit_ids_by_key = {
            str(value["key"]): _id("unit") for value in outline_draft["units"]
        }
        units = tuple(
            ConfirmationUnit(
                unit_id=unit_ids_by_key[str(value["key"])],
                outline_id=outline_id,
                sequence=sequence,
                title=str(value["title"]),
                node_ids=tuple(
                    node_ids_by_key[str(key)] for key in value["node_keys"]
                ),
                depends_on_unit_ids=tuple(
                    unit_ids_by_key[str(key)]
                    for key in value.get("depends_on_unit_keys", ())
                ),
            )
            for sequence, value in enumerate(outline_draft["units"], start=1)
        )
        outline = OutlineVersion(
            outline_id=outline_id,
            task_id=task.task_id,
            version=outline_version,
            title=outline_draft["title"],
            status=OutlineStatus.PENDING_CONFIRMATION,
            nodes=tuple(nodes),
            confirmation_units=units,
        )
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.OUTLINE_REVIEW,
            current_version=task.version,
            expected_version=task.version,
        )
        task.version += 1
        task.current_outline_version = outline_version
        task.updated_at = datetime.now(timezone.utc)
        run.status = RunStatus.WAITING_USER
        self.repository.save_outline(outline)
        self.repository.save_task(task)
        self.repository.save_run(run)
        self._event(task.task_id, "OutlineGenerated", {"outline_version": outline.version})
        self._checkpoint(task, run, "WAIT_OUTLINE_CONFIRMATION")
        return self.repository.snapshot(task.task_id)

    def confirm_outline(self, command: ConfirmOutline) -> WorkflowSnapshot:
        input_hash = sha256_json(
            {
                "command": "ConfirmOutline",
                "task_id": command.task_id,
                "outline_version": command.outline_version,
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict("idempotency key was already used with different input")
            return self._owned_snapshot(replay.task_id, command.actor_id)
        snapshot = self._owned_snapshot(command.task_id, command.actor_id)
        task = snapshot.task
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status != TaskStatus.OUTLINE_REVIEW or not snapshot.current_outline:
            raise InvalidTransition("task is not waiting for outline confirmation")
        outline = snapshot.current_outline
        self.outline_policy.confirm(outline, command.outline_version)

        old_run = snapshot.run
        old_run.status = RunStatus.SUCCEEDED
        old_run.ended_at = datetime.now(timezone.utc)
        self.repository.save_run(old_run)
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=old_run.thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.save_run(run)
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        outline.status = OutlineStatus.CONFIRMED
        outline.confirmed_at = datetime.now(timezone.utc)
        unit = outline.confirmation_units[0]
        unit.status = UnitStatus.GENERATING
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.GENERATING,
            current_version=task.version,
            expected_version=command.expected_task_version,
        )
        task.version += 1
        task.current_unit_sequence = 1
        task.updated_at = datetime.now(timezone.utc)
        self.repository.save_outline(outline)
        self.repository.save_task(task)
        self._event(task.task_id, "OutlineConfirmed", {"outline_version": outline.version})

        brief = snapshot.current_brief.brief
        return self._generate_unit(task, run, outline, unit, brief)

    def _generate_unit(self, task, run, outline, unit, brief) -> WorkflowSnapshot:
        try:
            return self._generate_unit_inner(
                task,
                run,
                outline,
                unit,
                brief,
            )
        except Exception as exc:
            if run.status != RunStatus.FAILED:
                unit.status = UnitStatus.FAILED
                outline.confirmation_units = tuple(
                    unit if item.unit_id == unit.unit_id else item
                    for item in outline.confirmation_units
                )
                task.status = self.task_policy.transition(
                    task.status,
                    TaskStatus.FAILED,
                    current_version=task.version,
                    expected_version=task.version,
                )
                task.version += 1
                task.updated_at = datetime.now(timezone.utc)
                run.status = RunStatus.FAILED
                run.error = str(exc)
                run.ended_at = datetime.now(timezone.utc)
                self.repository.save_outline(outline)
                self.repository.save_task(task)
                self.repository.save_run(run)
                self._event(
                    task.task_id,
                    "RunFailed",
                    {"node": "GENERATE_FIRST_UNIT"},
                )
                self._checkpoint(
                    task,
                    run,
                    "GENERATE_FIRST_UNIT_FAILED",
                )
            raise

    def _generate_unit_inner(
        self,
        task,
        run,
        outline,
        unit,
        brief,
    ) -> WorkflowSnapshot:
        investigation_context = None
        if self.unit_context_provider is not None:
            # Investigation uses an independent transaction/connection. Persist the
            # resumable workflow checkpoint first so FK checks cannot wait on this
            # command's own uncommitted Task/Unit rows.
            commit = getattr(self.repository, "commit", None)
            if commit is not None:
                commit()
            investigation_context = self.unit_context_provider(
                task=task,
                run=run,
                outline=outline,
                unit=unit,
                brief=brief,
            )
        nodes_by_key = {
            (node.stable_key or node.node_id): node
            for node in outline.nodes
            if not unit.node_ids or node.node_id in unit.node_ids
        }
        try:
            draft = invoke_validated(
                self.model,
                "generate_confirmation_unit",
                {
                    "requirement_brief": brief.as_dict(),
                    "outline": {
                        "title": outline.title,
                        "version": outline.version,
                        "nodes": [
                            {
                                "node_id": node.node_id,
                                "node_key": node.stable_key,
                                "sequence": node.sequence,
                                "title": node.title,
                                "purpose": node.purpose,
                            }
                            for node in outline.nodes
                            if not unit.node_ids or node.node_id in unit.node_ids
                        ],
                    },
                    "unit": {"sequence": unit.sequence, "title": unit.title},
                    "confirmed_context": build_confirmed_context(
                        unit,
                        self.repository.snapshot(task.task_id).sections,
                    ),
                    "investigation_context": investigation_context,
                },
                lambda value: validate_unit_draft_for_nodes(
                    value,
                    tuple(nodes_by_key),
                ),
            )
        except ModelOutputError as exc:
            unit.status = UnitStatus.FAILED
            outline.confirmation_units = tuple(
                unit if item.unit_id == unit.unit_id else item
                for item in outline.confirmation_units
            )
            task.status = self.task_policy.transition(
                task.status,
                TaskStatus.FAILED,
                current_version=task.version,
                expected_version=task.version,
            )
            task.version += 1
            task.updated_at = datetime.now(timezone.utc)
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.ended_at = datetime.now(timezone.utc)
            self.repository.save_outline(outline)
            self.repository.save_task(task)
            self.repository.save_run(run)
            self._event(task.task_id, "RunFailed", {"node": "GENERATE_FIRST_UNIT"})
            self._checkpoint(task, run, "GENERATE_FIRST_UNIT_FAILED")
            raise
        content = draft["content"]
        claims = draft["claims"]
        if (
            self.grounding_service is not None
            and not claims
            and self._contains_unlisted_current_state_claim(content)
        ):
            claims = (
                PrdClaim(
                    claim_id="claim-inventory-" + self._content_hash(content)[7:23],
                    text=content,
                    kind=ClaimKind.CURRENT_STATE,
                    criticality=ClaimCriticality.CRITICAL,
                ),
            )
        section_drafts = draft["sections"]
        if section_drafts:
            unit.section_drafts = tuple(
                {
                    "node_id": nodes_by_key[item["node_key"]].node_id,
                    "title": nodes_by_key[item["node_key"]].title,
                    "content": item["content"],
                    "content_hash": self._content_hash(item["content"]),
                }
                for item in section_drafts
            )
        elif len(nodes_by_key) == 1:
            node = next(iter(nodes_by_key.values()))
            unit.section_drafts = (
                {
                    "node_id": node.node_id,
                    "title": unit.title,
                    "content": content,
                    "content_hash": self._content_hash(content),
                },
            )
        if self.grounding_service is not None:
            context = investigation_context or {}
            grounding = self.grounding_service.ground(
                GroundingRequest(
                    grounding_run_id=_id("grounding"),
                    task_id=task.task_id,
                    unit_id=unit.unit_id,
                    repository_id=str(context.get("repository_id", "local")),
                    resolved_commit_sha=str(
                        context.get("resolved_commit_sha", "0" * 40)
                    ),
                    content=content,
                    claims=claims,
                    facts=tuple(context.get("facts", ())),
                    evidence=tuple(context.get("evidence", ())),
                    conflicts=tuple(context.get("conflicts", ())),
                )
            )
            save_grounding = getattr(
                self.repository,
                "save_grounding_result",
                None,
            )
            if save_grounding is None:
                raise RuntimeError(
                    "workflow repository does not support grounding results"
                )
            save_grounding(task.task_id, grounding)
            unit.grounding_run_id = grounding.grounding_run_id
            if not grounding.confirmable:
                unit.content = None
                unit.content_hash = None
                unit.section_drafts = ()
                unit.status = UnitStatus.HUMAN_INPUT_REQUIRED
                unit.version += 1
                outline.confirmation_units = tuple(
                    unit if item.unit_id == unit.unit_id else item
                    for item in outline.confirmation_units
                )
                run.status = RunStatus.WAITING_USER
                self.repository.save_outline(outline)
                self.repository.save_run(run)
                self._event(
                    task.task_id,
                    "UnitGroundingBlocked",
                    {"unit_id": unit.unit_id, "issues": len(grounding.issues)},
                )
                self._checkpoint(task, run, "WAIT_GROUNDING_INPUT")
                return self.repository.snapshot(task.task_id)
        unit.content = content
        unit.content_hash = self._content_hash(content)
        unit.status = UnitStatus.PENDING_CONFIRMATION
        unit.version += 1
        outline.confirmation_units = tuple(
            unit if item.unit_id == unit.unit_id else item
            for item in outline.confirmation_units
        )
        run.status = RunStatus.WAITING_USER
        self.repository.save_outline(outline)
        self.repository.save_run(run)
        self._event(task.task_id, "UnitGenerated", {"unit_id": unit.unit_id})
        self._checkpoint(task, run, "WAIT_UNIT_CONFIRMATION")
        return self.repository.snapshot(task.task_id)

    def confirm_unit(self, command: ConfirmUnit) -> WorkflowSnapshot:
        input_hash = sha256_json(
            {
                "command": "ConfirmUnit",
                "task_id": command.task_id,
                "unit_id": command.unit_id,
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict("idempotency key was already used with different input")
            return self._owned_snapshot(replay.task_id, command.actor_id)
        snapshot = self._owned_snapshot(command.task_id, command.actor_id)
        task = snapshot.task
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status != TaskStatus.GENERATING or not snapshot.current_outline:
            raise InvalidTransition("task is not waiting for unit confirmation")
        outline = snapshot.current_outline
        self.unit_policy.confirm(outline, command.unit_id, task.current_unit_sequence)

        old_run = snapshot.run
        old_run.status = RunStatus.SUCCEEDED
        old_run.ended_at = datetime.now(timezone.utc)
        self.repository.save_run(old_run)
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=old_run.thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.save_run(run)
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        unit = next(item for item in outline.confirmation_units if item.unit_id == command.unit_id)
        unit.status = UnitStatus.CONFIRMED
        unit.version += 1
        outline.confirmation_units = tuple(
            unit if item.unit_id == unit.unit_id else item
            for item in outline.confirmation_units
        )
        drafts = unit.section_drafts or (
            {
                "node_id": unit.node_ids[0] if unit.node_ids else None,
                "title": unit.title,
                "content": unit.content or "",
                "content_hash": unit.content_hash
                or self._content_hash(unit.content or ""),
            },
        )
        new_sections = tuple(
            PrdSectionVersion(
                section_id=_id("section"),
                unit_id=unit.unit_id,
                version=max(
                    (
                        item.version
                        for item in snapshot.sections
                        if item.node_id == draft["node_id"]
                    ),
                    default=0,
                )
                + 1,
                title=draft["title"],
                content=draft["content"],
                node_id=draft["node_id"],
                content_hash=draft["content_hash"],
                source_run_id=run.run_id,
                confirmed_by=command.actor_id,
                confirmed_at=datetime.now(timezone.utc),
            )
            for draft in drafts
        )
        self.repository.save_outline(outline)
        for section in new_sections:
            self.repository.save_section(task.task_id, section)
        self._event(task.task_id, "UnitConfirmed", {"unit_id": unit.unit_id})
        next_units = [
            item
            for item in outline.confirmation_units
            if item.sequence > unit.sequence
            and item.status in {UnitStatus.PENDING, UnitStatus.REVISION_REQUIRED}
        ]
        if next_units:
            next_unit = sorted(next_units, key=lambda item: item.sequence)[0]
            next_unit.status = UnitStatus.GENERATING
            outline.confirmation_units = tuple(
                next_unit if item.unit_id == next_unit.unit_id else item
                for item in outline.confirmation_units
            )
            task.version += 1
            task.current_unit_sequence = next_unit.sequence
            task.updated_at = datetime.now(timezone.utc)
            self.repository.save_outline(outline)
            self.repository.save_task(task)
            return self._generate_unit(
                task,
                run,
                outline,
                next_unit,
                snapshot.current_brief.brief,
            )

        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.FINAL_REVIEW,
            current_version=task.version,
            expected_version=command.expected_task_version,
        )
        task.version += 1
        task.updated_at = datetime.now(timezone.utc)
        self.repository.save_task(task)
        sections = self._current_sections(
            outline,
            tuple((*snapshot.sections, *new_sections)),
        )
        markdown = render_markdown(
            snapshot.current_brief.brief,
            outline,
            sections,
            render_evidence_appendix(snapshot.grounding_results),
        )
        document = PrdDocumentVersion(
            document_id=_id("document"),
            task_id=task.task_id,
            version=len(snapshot.documents) + 1,
            markdown=markdown,
            content_hash=self._content_hash(markdown),
            section_ids=tuple(item.section_id for item in sections),
        )
        self.repository.save_document(document)
        if self.quality_service is not None:
            quality = self.quality_service.check_document(
                quality_run_id=_id("quality"),
                task_id=task.task_id,
                document_id=document.document_id,
                outline=outline,
                sections=sections,
            )
            save_quality = getattr(self.repository, "save_quality_result", None)
            if save_quality is None:
                raise RuntimeError("workflow repository does not support quality results")
            save_quality(task.task_id, quality)
            self._event(
                task.task_id,
                "DocumentQualityChecked",
                {
                    "document_id": document.document_id,
                    "confirmable": quality.confirmable,
                    "issue_count": len(quality.issues),
                },
            )
        self._event(
            task.task_id,
            "PrdRendered",
            {"document_version": len(snapshot.documents) + 1},
        )
        run.status = RunStatus.SUCCEEDED
        run.ended_at = datetime.now(timezone.utc)
        self.repository.save_run(run)
        self._checkpoint(task, run, "FINAL_REVIEW")
        return self.repository.snapshot(task.task_id)

    def approve_revision_plan(
        self, command: ApproveRevisionPlan
    ) -> WorkflowSnapshot:
        input_hash = sha256_json(
            {
                "command": "ApproveRevisionPlan",
                "task_id": command.task_id,
                "document_id": command.document_id,
                "issue_ids": list(command.issue_ids),
                "unit_ids": list(command.unit_ids),
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict(
                    "idempotency key was already used with different input"
                )
            return self._owned_snapshot(replay.task_id, command.actor_id)
        snapshot = self._owned_snapshot(command.task_id, command.actor_id)
        task = snapshot.task
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status != TaskStatus.FINAL_REVIEW or not snapshot.current_outline:
            raise InvalidTransition("revision can only be approved during final review")
        if not snapshot.documents or snapshot.documents[-1].document_id != command.document_id:
            raise VersionConflict("revision plan is based on a stale document")
        relevant_quality = [
            item
            for item in snapshot.quality_results
            if item.scope_id == command.document_id
        ]
        if not relevant_quality:
            raise InvalidTransition("document has no quality issues to revise")
        issues = {
            issue.issue_id: issue for issue in relevant_quality[-1].issues
        }
        if not command.issue_ids or any(item not in issues for item in command.issue_ids):
            raise InvalidTransition("revision references unknown quality issues")
        allowed_units = {
            unit_id
            for issue_id in command.issue_ids
            for unit_id in issues[issue_id].affected_unit_ids
        }
        if not command.unit_ids or any(item not in allowed_units for item in command.unit_ids):
            raise InvalidTransition("revision units are not affected by approved issues")

        outline = snapshot.current_outline
        reopen_ids = self._dependent_unit_closure(
            outline,
            set(command.unit_ids),
        )
        selected = [
            item
            for item in outline.confirmation_units
            if item.unit_id in reopen_ids
        ]
        if not set(command.unit_ids).issubset(
            {item.unit_id for item in selected}
        ):
            raise InvalidTransition("revision unit does not belong to current outline")
        target = sorted(selected, key=lambda item: item.sequence)[0]
        for item in selected:
            item.status = UnitStatus.REVISION_REQUIRED
        target.status = UnitStatus.GENERATING
        outline.confirmation_units = tuple(
            item for item in outline.confirmation_units
        )
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.GENERATING,
            current_version=task.version,
            expected_version=command.expected_task_version,
        )
        task.version += 1
        task.current_unit_sequence = target.sequence
        task.updated_at = datetime.now(timezone.utc)
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=snapshot.run.thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.save_run(run)
        self.repository.save_outline(outline)
        self.repository.save_task(task)
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        self._event(
            task.task_id,
            "RevisionPlanApproved",
            {
                "document_id": command.document_id,
                "unit_ids": [
                    item.unit_id
                    for item in sorted(selected, key=lambda value: value.sequence)
                ],
            },
        )
        return self._generate_unit(
            task,
            run,
            outline,
            target,
            snapshot.current_brief.brief,
        )

    def reopen_prd(self, command: ReopenPrd) -> WorkflowSnapshot:
        reason = command.reason.strip()
        if not reason:
            raise InvalidCommand("reopen reason cannot be empty")
        input_hash = sha256_json(
            {
                "command": "ReopenPrd",
                "task_id": command.task_id,
                "unit_ids": list(command.unit_ids),
                "reason": reason,
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict(
                    "idempotency key was already used with different input"
                )
            return self._owned_snapshot(replay.task_id, command.actor_id)
        snapshot = self._owned_snapshot(command.task_id, command.actor_id)
        task = snapshot.task
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status != TaskStatus.COMPLETED or not snapshot.current_outline:
            raise InvalidTransition("only a completed PRD can be reopened")
        outline = snapshot.current_outline
        known_ids = {item.unit_id for item in outline.confirmation_units}
        selected_ids = set(command.unit_ids)
        if not selected_ids or not selected_ids.issubset(known_ids):
            raise InvalidTransition("reopen units must belong to the current outline")
        reopen_ids = self._dependent_unit_closure(outline, selected_ids)
        reopened = []
        for unit in outline.confirmation_units:
            if unit.unit_id in reopen_ids:
                unit.status = UnitStatus.REVISION_REQUIRED
                reopened.append(unit)
        target = min(reopened, key=lambda item: item.sequence)
        target.status = UnitStatus.GENERATING
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.GENERATING,
            current_version=task.version,
            expected_version=command.expected_task_version,
        )
        task.version += 1
        task.current_unit_sequence = target.sequence
        task.updated_at = datetime.now(timezone.utc)
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=snapshot.run.thread_id,
            status=RunStatus.RUNNING,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
        )
        self.repository.save_run(run)
        self.repository.save_outline(outline)
        self.repository.save_task(task)
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        self._event(
            task.task_id,
            "PrdReopened",
            {
                "unit_ids": [
                    item.unit_id
                    for item in sorted(reopened, key=lambda value: value.sequence)
                ],
                "reason": reason,
            },
        )
        return self._generate_unit(
            task,
            run,
            outline,
            target,
            snapshot.current_brief.brief,
        )

    def finalize_prd(self, command: FinalizePrd) -> WorkflowSnapshot:
        input_hash = sha256_json(
            {
                "command": "FinalizePrd",
                "task_id": command.task_id,
                "document_id": command.document_id,
                "content_hash": command.content_hash,
                "expected_task_version": command.expected_task_version,
            }
        )
        replay = self.repository.get_idempotency(command.actor_id, command.idempotency_key)
        if replay:
            if replay.input_hash != input_hash:
                raise IdempotencyConflict(
                    "idempotency key was already used with different input"
                )
            return self._owned_snapshot(replay.task_id, command.actor_id)
        snapshot = self._owned_snapshot(command.task_id, command.actor_id)
        task = snapshot.task
        if task.version != command.expected_task_version:
            raise VersionConflict(
                f"expected task version {command.expected_task_version}, "
                f"current version is {task.version}"
            )
        if task.status != TaskStatus.FINAL_REVIEW or not snapshot.documents:
            raise InvalidTransition("task is not ready for final confirmation")
        document = snapshot.documents[-1]
        if (
            document.document_id != command.document_id
            or document.content_hash != command.content_hash
        ):
            raise VersionConflict("document version or content hash is stale")
        if any(
            unit.status != UnitStatus.CONFIRMED
            for unit in snapshot.current_outline.confirmation_units
        ):
            raise InvalidTransition("all confirmation units must be confirmed")
        relevant_quality = [
            item
            for item in snapshot.quality_results
            if item.scope_id == document.document_id
        ]
        if relevant_quality and not relevant_quality[-1].confirmable:
            raise InvalidTransition("document quality issues must be resolved")
        run = AgentRun(
            run_id=_id("run"),
            task_id=task.task_id,
            thread_id=snapshot.run.thread_id,
            status=RunStatus.SUCCEEDED,
            input_hash=input_hash,
            idempotency_key=command.idempotency_key,
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
        )
        document = replace(
            document,
            status=DocumentStatus.CONFIRMED,
            confirmed_by=command.actor_id,
            confirmed_at=datetime.now(timezone.utc),
        )
        task.status = self.task_policy.transition(
            task.status,
            TaskStatus.COMPLETED,
            current_version=task.version,
            expected_version=command.expected_task_version,
        )
        task.version += 1
        task.updated_at = datetime.now(timezone.utc)
        self.repository.save_run(run)
        self.repository.save_document(document)
        self.repository.save_task(task)
        self.repository.save_idempotency(
            IdempotencyRecord(
                command.actor_id,
                command.idempotency_key,
                input_hash,
                task.task_id,
                run.run_id,
            )
        )
        self._event(task.task_id, "PrdFinalized", {"document_id": document.document_id})
        self._checkpoint(task, run, "COMPLETED")
        return self.repository.snapshot(task.task_id)

    @staticmethod
    def _content_hash(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _contains_unlisted_current_state_claim(value: str) -> bool:
        lowered = value.lower()
        return any(
            marker in lowered
            for marker in (
                "当前系统",
                "现有系统",
                "目前系统",
                "已经实现",
                "当前支持",
                "currently",
                "existing system",
            )
        )

    @staticmethod
    def _current_sections(outline, sections):
        latest_by_node = {}
        for section in sections:
            key = section.node_id or section.unit_id
            current = latest_by_node.get(key)
            if current is None or section.version > current.version:
                latest_by_node[key] = section
        ordered = []
        for node in sorted(outline.nodes, key=lambda item: item.sequence):
            if node.node_id in latest_by_node:
                ordered.append(latest_by_node[node.node_id])
        if ordered:
            return tuple(ordered)
        return tuple(
            latest_by_node[unit.unit_id]
            for unit in sorted(outline.confirmation_units, key=lambda item: item.sequence)
            if unit.unit_id in latest_by_node
        )

    @staticmethod
    def _dependent_unit_closure(outline, selected_ids):
        closure = set(selected_ids)
        changed = True
        while changed:
            changed = False
            for unit in outline.confirmation_units:
                if unit.unit_id in closure:
                    continue
                if any(item in closure for item in unit.depends_on_unit_ids):
                    closure.add(unit.unit_id)
                    changed = True
        return closure

    def show(self, task_id: str, actor_id: str = "local-user") -> WorkflowSnapshot:
        return self._owned_snapshot(task_id, actor_id)

    def _owned_task(self, task_id: str, actor_id: str) -> Task:
        task = self.repository.get_task(task_id)
        if task.owner_id != actor_id:
            raise NotFound(f"task not found: {task_id}")
        return task

    def _owned_snapshot(self, task_id: str, actor_id: str) -> WorkflowSnapshot:
        scoped = getattr(self.repository, "snapshot_for_owner", None)
        if scoped is not None:
            return scoped(task_id, actor_id)
        self._owned_task(task_id, actor_id)
        return self.repository.snapshot(task_id)

    def _checkpoint(self, task: Task, run: AgentRun, current_node: str) -> None:
        self.repository.save_checkpoint(
            run.thread_id,
            {
                "task_id": task.task_id,
                "run_id": run.run_id,
                "thread_id": run.thread_id,
                "graph_version": run.graph_version,
                "task_version": task.version,
                "current_node": current_node,
            },
        )

    def _event(self, task_id: str, event_type: str, payload: dict) -> None:
        append = getattr(self.repository, "append_event", None)
        if append:
            append(task_id, event_type, payload)
