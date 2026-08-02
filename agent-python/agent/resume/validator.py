from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from agent.checkpoint import CheckpointCodec, CheckpointError
from agent.context import RunContext
from agent.graph.snapshot import LoopCheckpointStatus, LoopSnapshot
from agent.result import AgentResult, SubmissionDisposition

from .artifacts import index_artifacts
from .hashes import HashDigest
from .models import ValidatedRunState
from agent.runtime.idempotency import outcome_artifact_key


class ResumeValidationError(CheckpointError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ResumeValidator:
    checkpoint_codec: CheckpointCodec

    def hydrate(self, context: RunContext) -> ValidatedRunState:
        version = context.workflow_version or "agent-runtime.v1"
        if version not in {"agent-runtime.v1", "agent-runtime.v4"}:
            raise ResumeValidationError("WORKFLOW_VERSION_UNSUPPORTED")
        if (
            version == "agent-runtime.v4"
            and context.resume_draft is not None
            and context.base_draft is None
            and context.submitted_draft is None
        ):
            raise ResumeValidationError("DRAFT_RECEIPT_MISMATCH")
        self._validate_repository_pair(context)
        self._validate_ledger_contract(context)
        if not context.checkpoint:
            if context.checkpoint_sequence:
                raise ResumeValidationError("CHECKPOINT_SEQUENCE_MISMATCH")
            if version == "agent-runtime.v4":
                if context.submitted_draft:
                    raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
                if context.execution_ledger_version:
                    self._validate_ledger_owned_durable_state(context)
                elif context.resume_evidence or context.resume_artifacts:
                    raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
            return ValidatedRunState(
                context=context,
                status=LoopCheckpointStatus.INITIALIZED,
                state=None,
            )
        checkpoint = self.checkpoint_codec.decode(context.checkpoint)
        if checkpoint.sequence != context.checkpoint_sequence:
            raise ResumeValidationError("CHECKPOINT_SEQUENCE_MISMATCH")
        if checkpoint.run_id != context.run_id:
            raise ResumeValidationError("RUN_IDENTITY_MISMATCH")
        if checkpoint.workflow_version != version:
            raise ResumeValidationError("WORKFLOW_VERSION_UNSUPPORTED")
        schema = checkpoint.payload.get("snapshot_schema_version")
        if version == "agent-runtime.v1":
            if schema not in {None, "agent-loop-snapshot.v1", "agent-loop-snapshot.v2"}:
                raise ResumeValidationError("SNAPSHOT_SCHEMA_UNSUPPORTED")
            snapshot = LoopSnapshot.from_payload(checkpoint.payload)
            state = dict(snapshot.state)
            try:
                task_version = int(state.get("task_version", 0))
            except (TypeError, ValueError) as error:
                raise ResumeValidationError("TASK_VERSION_MISMATCH") from error
            if state.get("run_id") != context.run_id:
                raise ResumeValidationError("RUN_IDENTITY_MISMATCH")
            if state.get("task_id") != context.task_id:
                raise ResumeValidationError("TASK_IDENTITY_MISMATCH")
            if task_version != max(context.task_version, 1):
                raise ResumeValidationError("TASK_VERSION_MISMATCH")
            self._hydrate_draft_artifact(state, context.resume_artifacts)
            return ValidatedRunState(context, snapshot.status, state)
        if schema != "agent-loop-snapshot.v3":
            raise ResumeValidationError("SNAPSHOT_SCHEMA_UNSUPPORTED")
        return self._hydrate_v3(context, checkpoint)

    def _hydrate_v3(self, context: RunContext, checkpoint) -> ValidatedRunState:
        snapshot = LoopSnapshot.from_payload(checkpoint.payload)
        state = dict(snapshot.state)
        identity = checkpoint.payload.get("identity")
        progress = checkpoint.payload.get("progress")
        if not isinstance(identity, dict) or not isinstance(progress, dict):
            raise ResumeValidationError("SNAPSHOT_SCHEMA_UNSUPPORTED")
        if identity.get("run_id") != context.run_id:
            raise ResumeValidationError("RUN_IDENTITY_MISMATCH")
        if identity.get("task_id") != context.task_id:
            raise ResumeValidationError("TASK_IDENTITY_MISMATCH")
        if identity.get("workflow_version") != "agent-runtime.v4":
            raise ResumeValidationError("WORKFLOW_VERSION_UNSUPPORTED")
        if int(identity.get("base_task_version", -1)) != max(context.task_version, 1):
            raise ResumeValidationError("TASK_VERSION_MISMATCH")
        if identity.get("repository_binding_id", "") != context.repository_binding_id or identity.get("repository_revision", "") != context.repository_revision:
            raise ResumeValidationError("REPOSITORY_SNAPSHOT_MISMATCH")
        if int(checkpoint.payload.get("checkpoint_sequence", -1)) != checkpoint.sequence:
            raise ResumeValidationError("CHECKPOINT_SEQUENCE_MISMATCH")
        if checkpoint.payload.get("execution_ledger_version", "") != context.execution_ledger_version:
            raise ResumeValidationError("LEDGER_VERSION_MISMATCH")
        terminal_keys = checkpoint.payload.get("terminal_operation_keys", [])
        if not isinstance(terminal_keys, list) or len(terminal_keys) != len(set(terminal_keys)):
            raise ResumeValidationError("LEDGER_ENTRY_INVALID")
        durable_terminal = {
            entry.operation_key
            for entry in context.ledger_entries
            if entry.status in {"SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"}
        }
        if not set(terminal_keys).issubset(durable_terminal):
            raise ResumeValidationError("LEDGER_ENTRY_INVALID")
        summary = context.resume_summary
        if summary is None:
            raise ResumeValidationError("RESUME_CONTEXT_LIMIT_EXCEEDED")
        if HashDigest.parse(summary.checkpoint_content_hash) != HashDigest.of_bytes(
            context.checkpoint
        ):
            raise ResumeValidationError("CHECKPOINT_HASH_MISMATCH")
        for name, value in progress.items():
            if not isinstance(value, int) or value < 0:
                raise ResumeValidationError("COUNTER_REGRESSION")
        if progress.get("tool_call_count", 0) > progress.get("iteration", 0) + progress.get("supplement_count", 0):
            raise ResumeValidationError("COUNTER_REGRESSION")
        if progress.get("replan_count", 0) > progress.get("iteration", 0):
            raise ResumeValidationError("COUNTER_REGRESSION")
        model_attempt_count = progress.get("model_attempt_count", 0)
        ledger_model_attempt_count = sum(
            1
            for entry in context.ledger_entries
            if entry.entry_kind == "MODEL"
            and entry.status in {"SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"}
        )
        durable_model_attempt_count = max(
            summary.terminal_model_attempt_count, ledger_model_attempt_count
        )
        if model_attempt_count > durable_model_attempt_count:
            raise ResumeValidationError("COUNTER_REGRESSION")
        if model_attempt_count < durable_model_attempt_count and not context.execution_ledger_version:
            raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
        self._validate_unique(state.get("completed_action_signatures", []))
        evidence_refs = state.get("evidence_refs", [])
        self._validate_unique(evidence_refs)
        durable_refs = {_evidence_reference(item) for item in context.resume_evidence}
        if summary.evidence_count != len(durable_refs) or set(summary.evidence_refs) != durable_refs:
            raise ResumeValidationError("EVIDENCE_INDEX_INVALID")
        if not set(evidence_refs).issubset(durable_refs):
            raise ResumeValidationError("APPEND_ONLY_SET_SHRUNK")
        if durable_refs != set(evidence_refs) and not context.execution_ledger_version:
            raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
        artifacts = index_artifacts(context.resume_artifacts)
        if summary.artifact_count != len(artifacts):
            raise ResumeValidationError("ARTIFACT_MISSING")
        summary_artifacts = {item.artifact_key: item for item in summary.artifacts}
        if set(summary_artifacts) != set(artifacts):
            raise ResumeValidationError("ARTIFACT_MISSING")
        for key, artifact in artifacts.items():
            identity = summary_artifacts[key]
            if (
                identity.artifact_type != artifact.artifact_type
                or identity.generation != artifact.generation
                or identity.request_hash != artifact.request_hash
                or HashDigest.parse(identity.content_hash)
                != HashDigest.parse(artifact.content_hash)
            ):
                raise ResumeValidationError("ARTIFACT_HASH_MISMATCH")
        required = checkpoint.payload.get("required_artifacts", [])
        required_keys = []
        for item in required:
            if not isinstance(item, dict) or not item.get("artifact_key"):
                raise ResumeValidationError("ARTIFACT_MISSING")
            key = item["artifact_key"]
            required_keys.append(key)
            artifact = artifacts.get(key)
            if artifact is None:
                raise ResumeValidationError("ARTIFACT_MISSING")
            if HashDigest.parse(item["content_hash"]) != HashDigest.parse(artifact.content_hash):
                raise ResumeValidationError("ARTIFACT_HASH_MISMATCH")
            if (
                item.get("artifact_type") != artifact.artifact_type
                or int(item.get("generation", -1)) != artifact.generation
                or item.get("request_hash") != artifact.request_hash
            ):
                raise ResumeValidationError("ARTIFACT_HASH_MISMATCH")
        self._validate_unique(required_keys)
        extra_artifacts = set(artifacts) - set(required_keys)
        if extra_artifacts and not self._ledger_artifact_keys(context).issuperset(extra_artifacts):
            raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
        self._hydrate_draft_artifact(state, tuple(artifacts.values()))
        self._validate_revision_scope(context, checkpoint.payload)
        disposition = SubmissionDisposition.NOT_READY
        if snapshot.status is LoopCheckpointStatus.READY_TO_SUBMIT:
            disposition = self._validate_submission(context, checkpoint.payload)
        state["checkpoint_sequence"] = checkpoint.sequence
        return ValidatedRunState(context, snapshot.status, state, disposition)

    @staticmethod
    def _validate_repository_pair(context: RunContext) -> None:
        if bool(context.repository_binding_id) != bool(context.repository_revision):
            raise ResumeValidationError("REPOSITORY_SNAPSHOT_MISMATCH")

    @staticmethod
    def _validate_ledger_contract(context: RunContext) -> None:
        if not context.execution_ledger_version:
            if context.run_budget is not None or context.consumed_budget is not None or context.ledger_entries:
                raise ResumeValidationError("LEDGER_VERSION_MISMATCH")
            return
        if context.workflow_version != "agent-runtime.v4" or context.execution_ledger_version != "run-ledger.v1":
            raise ResumeValidationError("LEDGER_VERSION_MISMATCH")
        if context.run_budget is None or context.consumed_budget is None:
            raise ResumeValidationError("LEDGER_CONTEXT_INCOMPLETE")
        identities: dict[str, tuple[str, str, str]] = {}
        valid_statuses = {"RESERVED", "CALL_STARTED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"}
        for entry in context.ledger_entries:
            identity = (entry.entry_kind, entry.operation, entry.request_hash)
            previous = identities.setdefault(entry.operation_key, identity)
            if not entry.operation_key or previous != identity or entry.status not in valid_statuses:
                raise ResumeValidationError("LEDGER_ENTRY_INVALID")

    @classmethod
    def _validate_ledger_owned_durable_state(cls, context: RunContext) -> None:
        artifacts = index_artifacts(context.resume_artifacts)
        allowed = cls._ledger_artifact_keys(context)
        for item in artifacts.values():
            if item.artifact_key in allowed:
                continue
            if cls._is_information_need_plan_ahead(context, item):
                continue
            raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")
        evidence_refs = cls._ledger_evidence_refs(context)
        if any(_evidence_reference(item) not in evidence_refs for item in context.resume_evidence):
            raise ResumeValidationError("DURABLE_STATE_AHEAD_OF_CHECKPOINT")

    @staticmethod
    def _is_information_need_plan_ahead(context: RunContext, artifact) -> bool:
        if (
            artifact.artifact_key != f"{context.run_id}:information_need:plan:1"
            or artifact.artifact_type != "INFORMATION_NEED_PLAN"
            or artifact.generation != 1
        ):
            return False
        owner = next(
            (
                entry
                for entry in context.ledger_entries
                if entry.entry_kind == "MODEL"
                and entry.operation == "plan_information_need"
                and entry.operation_key == "model:plan_information_need:plan1"
                and entry.status == "SUCCEEDED"
                and entry.request_hash == artifact.request_hash
            ),
            None,
        )
        if owner is None:
            return False
        try:
            from agent.information_need.artifact import decode_plan_artifact
            from agent.information_need.models import NeedPlanningContext
            from agent.information_need.policies import planning_context_hash

            scope = context.revision_scope
            revision_scope = (
                {
                    "base_draft_id": scope.base_draft_id,
                    "base_draft_hash": scope.base_draft_hash,
                    "reopened_unit_keys": list(scope.reopened_unit_keys),
                    "immutable_unit_keys": list(scope.immutable_unit_keys),
                    "user_feedback": scope.user_feedback,
                }
                if scope is not None
                else None
            )
            planning_context = NeedPlanningContext(
                run_id=context.run_id,
                task_id=context.task_id,
                task_message=context.task_message,
                task_version=max(context.task_version, 1),
                workflow_version=context.workflow_version,
                repository_binding_id=context.repository_binding_id,
                repository_revision=context.repository_revision,
                historical_prd_available=False,
                remaining_model_attempts=0,
                remaining_tool_calls=0,
                remaining_iterations=0,
                remaining_replans=0,
                revision_scope=revision_scope,
            )
            return (
                decode_plan_artifact(artifact).context_hash
                == planning_context_hash(planning_context)
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _ledger_artifact_keys(context: RunContext) -> set[str]:
        keys: set[str] = set()
        for entry in context.ledger_entries:
            if entry.entry_kind not in {"MODEL", "CAPABILITY"}:
                continue
            if entry.output_artifact_key:
                keys.add(entry.output_artifact_key)
            if entry.status in {"CALL_STARTED", "SUCCEEDED"}:
                keys.add(outcome_artifact_key(context.run_id, entry.operation_key))
        return keys

    @staticmethod
    def _ledger_evidence_refs(context: RunContext) -> set[str]:
        refs = {ref for entry in context.ledger_entries for ref in entry.evidence_refs}
        capability_requests = {
            entry.request_hash
            for entry in context.ledger_entries
            if entry.entry_kind == "CAPABILITY"
            and entry.status in {"CALL_STARTED", "SUCCEEDED"}
        }
        for artifact in context.resume_artifacts:
            if (
                artifact.artifact_type != "CAPABILITY_VALIDATED_OUTPUT"
                or artifact.request_hash not in capability_requests
            ):
                continue
            try:
                envelope = json.loads(artifact.content)
                items = envelope["value"]
            except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if envelope.get("schema") != "capability-evidence.v1" or not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw = "\x00".join(
                    str(item.get(key, ""))
                    for key in ("source_type", "source_id", "locator", "excerpt_hash")
                )
                refs.add("sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest())
        return refs

    @staticmethod
    def _hydrate_draft_artifact(state: dict, artifacts) -> None:
        draft_key = state.get("draft_artifact_key")
        if not draft_key or state.get("candidate_markdown"):
            return
        artifact = next(
            (item for item in artifacts if item.artifact_key == draft_key), None
        )
        if artifact is None:
            raise ResumeValidationError("ARTIFACT_MISSING")
        if HashDigest.of_bytes(artifact.content) != HashDigest.parse(
            state.get("draft_artifact_hash", "")
        ):
            raise ResumeValidationError("ARTIFACT_HASH_MISMATCH")
        try:
            bundle = json.loads(artifact.content)
            markdown = bundle["markdown"]
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResumeValidationError("ARTIFACT_SCHEMA_UNSUPPORTED") from error
        if not isinstance(markdown, str) or not markdown.strip():
            raise ResumeValidationError("ARTIFACT_SCHEMA_UNSUPPORTED")
        state["candidate_markdown"] = markdown

    @staticmethod
    def _validate_unique(values) -> None:
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise ResumeValidationError("APPEND_ONLY_SET_SHRUNK")

    @staticmethod
    def _validate_revision_scope(context: RunContext, payload: dict) -> None:
        immutable = payload.get("immutable_unit_keys", [])
        reopened = payload.get("reopened_unit_keys", [])
        if len(immutable) != len(set(immutable)) or len(reopened) != len(set(reopened)) or set(immutable) & set(reopened):
            raise ResumeValidationError("REVISION_SCOPE_MISMATCH")
        scope = context.revision_scope
        expected_immutable = sorted(scope.immutable_unit_keys) if scope else []
        expected_reopened = sorted(scope.reopened_unit_keys) if scope else []
        if sorted(immutable) != expected_immutable or sorted(reopened) != expected_reopened:
            raise ResumeValidationError("REVISION_SCOPE_MISMATCH")

    @staticmethod
    def _validate_submission(context: RunContext, payload: dict) -> SubmissionDisposition:
        submission = payload.get("submission")
        if not isinstance(submission, dict):
            raise ResumeValidationError("DRAFT_RECEIPT_MISMATCH")
        if int(submission.get("expected_task_version", -1)) != max(context.task_version, 1):
            raise ResumeValidationError("DRAFT_RECEIPT_MISMATCH")
        receipt = context.submitted_draft
        if receipt is None:
            return SubmissionDisposition.SUBMIT_REQUIRED
        if HashDigest.of_bytes(receipt.content) != HashDigest.parse(receipt.content_hash):
            raise ResumeValidationError("DRAFT_RECEIPT_MISMATCH")
        if (
            receipt.draft_key != submission.get("draft_key")
            or HashDigest.parse(receipt.content_hash) != HashDigest.parse(submission.get("draft_patch_hash", ""))
            or receipt.task_version != max(context.task_version, 1) + 1
        ):
            raise ResumeValidationError("DRAFT_RECEIPT_MISMATCH")
        return SubmissionDisposition.TERMINAL_ACK_ONLY


@dataclass(frozen=True)
class ValidatedAgentRuntime:
    validator: ResumeValidator
    loop: object

    def __call__(self, context: RunContext, cancel_event=None) -> AgentResult:
        return self.loop(self.validator.hydrate(context), cancel_event)


def _evidence_reference(item) -> str:
    raw = "\x00".join((item.source_type, item.source_id, item.locator, item.excerpt_hash))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
