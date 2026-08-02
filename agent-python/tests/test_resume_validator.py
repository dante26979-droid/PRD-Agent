from __future__ import annotations

import hashlib

import pytest

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph.snapshot import LoopCheckpointStatus, LoopSnapshot
from agent.graph.runtime import LangGraphAgentLoop
from agent.model import ModelResponse
from agent.quality import DraftQualityPolicy
from agent.resume.hashes import HashDigest
from agent.resume.validator import (
    ResumeValidationError,
    ResumeValidator,
    ValidatedAgentRuntime,
)
from agent.result import SubmissionDisposition
from agent.runtime import BufferedRuntimeEventSink
from agent.v1 import agent_execution_pb2 as proto


def _context(**overrides) -> RunContext:
    values = dict(
        run_id="run-1",
        tenant_id="tenant-1",
        owner_id="owner-1",
        task_id="task-1",
        task_message="write a PRD",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        checkpoint_sequence=0,
        task_version=3,
        repository_binding_id="binding-1",
        repository_revision="a" * 40,
    )
    values.update(overrides)
    if values["checkpoint"] and "resume_summary" not in overrides:
        artifacts = values.get("resume_artifacts", ())
        values["resume_summary"] = proto.ResumeStateSummary(
            checkpoint_content_hash=str(HashDigest.of_bytes(values["checkpoint"])),
            evidence_count=0,
            artifact_count=len(artifacts),
            artifacts=(
                proto.RunArtifactIdentity(
                    artifact_key=item.artifact_key,
                    artifact_type=item.artifact_type,
                    generation=item.generation,
                    request_hash=item.request_hash,
                    content_hash=item.content_hash,
                )
                for item in artifacts
            ),
        )
    return RunContext(**values)


def _checkpoint(status=LoopCheckpointStatus.INITIALIZED, **state_overrides):
    state = {
        "run_id": "run-1",
        "task_id": "task-1",
        "task_version": 3,
        "workflow_version": "agent-runtime.v4",
        "repository_binding_id": "binding-1",
        "repository_revision": "a" * 40,
        "checkpoint_sequence": 1,
        "iteration": 0,
        "tool_call_count": 0,
        "token_usage": 0,
        "replan_count": 0,
        "no_progress_rounds": 0,
        "supplement_count": 0,
        "repair_count": 0,
        "draft_generation": 0,
        "completed_action_signatures": [],
        "evidence_refs": [],
        "immutable_unit_keys": [],
        "reopened_unit_keys": [],
    }
    state.update(state_overrides)
    payload = LoopSnapshot(status, state).as_payload(
        workflow_version="agent-runtime.v4"
    )
    return CheckpointCodec().encode(
        workflow_version="agent-runtime.v4",
        run_id="run-1",
        task_version=3,
        sequence=1,
        payload=payload,
    )


def test_fresh_v4_context_is_validated_before_graph_entry():
    validated = ResumeValidator(CheckpointCodec()).hydrate(_context())
    assert validated.status is LoopCheckpointStatus.INITIALIZED
    assert validated.state is None


def test_v4_rejects_v2_snapshot_and_repository_revision_drift():
    legacy_payload = LoopSnapshot(
        LoopCheckpointStatus.INITIALIZED,
        {"run_id": "run-1", "task_id": "task-1", "task_version": 3},
    ).as_payload()
    legacy = CheckpointCodec().encode(
        workflow_version="agent-runtime.v4",
        run_id="run-1",
        task_version=3,
        sequence=1,
        payload=legacy_payload,
    )
    with pytest.raises(ResumeValidationError) as error:
        ResumeValidator(CheckpointCodec()).hydrate(
            _context(checkpoint=legacy, checkpoint_sequence=1)
        )
    assert error.value.reason_code == "SNAPSHOT_SCHEMA_UNSUPPORTED"

    checkpoint = _checkpoint()
    with pytest.raises(ResumeValidationError) as error:
        ResumeValidator(CheckpointCodec()).hydrate(
            _context(
                checkpoint=checkpoint,
                checkpoint_sequence=1,
                repository_revision="b" * 40,
            )
        )
    assert error.value.reason_code == "REPOSITORY_SNAPSHOT_MISMATCH"


def test_hash_digest_accepts_legacy_raw_and_rejects_noncanonical_values():
    raw = hashlib.sha256(b"payload").hexdigest()
    assert str(HashDigest.parse(raw)) == "sha256:" + raw
    assert HashDigest.parse(raw) == HashDigest.parse("sha256:" + raw)
    with pytest.raises(Exception):
        HashDigest.parse(raw.upper())


def test_artifact_hash_drift_fails_closed():
    content = b'{"markdown":"# PRD"}'
    content_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    checkpoint = _checkpoint(
        draft_artifact_key="run-1:draft-bundle:1",
        draft_artifact_hash=content_hash,
    )
    artifact = proto.RunArtifact(
        artifact_key="run-1:draft-bundle:1",
        artifact_type="DRAFT",
        generation=1,
        request_hash=content_hash,
        content_hash=content_hash,
        content=b"tampered",
    )
    with pytest.raises(Exception, match="artifact content hash mismatch"):
        ResumeValidator(CheckpointCodec()).hydrate(
            _context(
                checkpoint=checkpoint,
                checkpoint_sequence=1,
                resume_artifacts=(artifact,),
            )
        )


def test_matching_same_run_draft_receipt_selects_terminal_ack_only():
    patch = b'{"markdown":"# PRD","run_id":"run-1","task_id":"task-1"}'
    patch_hash = "sha256:" + hashlib.sha256(patch).hexdigest()
    submission = {
        "draft_key": "run-1:draft:1",
        "draft_patch_hash": patch_hash,
        "markdown_hash": "sha256:" + hashlib.sha256(b"# PRD").hexdigest(),
        "expected_task_version": 3,
    }
    checkpoint = _checkpoint(
        LoopCheckpointStatus.READY_TO_SUBMIT,
        candidate_markdown="# PRD",
        draft_hash=submission["markdown_hash"],
        submission=submission,
    )
    receipt = proto.SubmittedDraftReceipt(
        draft_key="run-1:draft:1",
        content_hash=patch_hash,
        task_version=4,
        content=patch,
    )
    validated = ResumeValidator(CheckpointCodec()).hydrate(
        _context(
            checkpoint=checkpoint,
            checkpoint_sequence=1,
            submitted_draft=receipt,
        )
    )
    assert (
        validated.submission_disposition
        is SubmissionDisposition.TERMINAL_ACK_ONLY
    )


def test_ready_draft_ack_recovery_uses_zero_model_capability_and_submit_calls():
    class TwoShotModel:
        calls = 0

        def complete(self, system_prompt, user_prompt):
            self.calls += 1
            if self.calls == 1:
                output = (
                    '{"question":"Is investigation needed?",'
                    '"suggested_requiredness":"NONE",'
                    '"need_kind":"NEW_BEHAVIOR",'
                    '"source_types":[],"fallback":"target state only"}'
                )
            else:
                output = '{"markdown":"# PRD\\n\\nready"}'
            return ModelResponse(
                output=output,
                token_usage={"total_tokens": 1},
                model_id="test-model",
            )

    first_model = TwoShotModel()
    first_sink = BufferedRuntimeEventSink()
    first = ValidatedAgentRuntime(
        ResumeValidator(CheckpointCodec()),
        LangGraphAgentLoop(
            model=first_model,
            checkpoint_codec=CheckpointCodec(),
            quality_policy=DraftQualityPolicy(),
            required_coverage=(),
        ),
    )(
        _context(
            repository_binding_id="",
            repository_revision="",
            execution_ledger_version="run-ledger.v1",
            run_budget=proto.RunBudget(
                max_model_attempts=4,
                max_tool_calls=2,
                max_iterations=3,
                max_replans=1,
                max_supplements=1,
                max_quality_repairs=1,
                max_input_tokens=20_000,
                max_output_tokens=4_000,
                max_elapsed_ms=60_000,
            ),
            consumed_budget=proto.ConsumedBudget(),
            event_sink=first_sink,
        )
    )
    first_checkpoint_sequence, first_checkpoint = first_sink.checkpoints[-1]
    terminal_entries = {}
    for event in first_sink.ledger_events:
        terminal_entries[event.entry.operation_key] = event.entry
    ledger_entries = tuple(terminal_entries[key] for key in sorted(terminal_entries))
    consumed = proto.ConsumedBudget()
    for entry in ledger_entries:
        if entry.status != "SUCCEEDED":
            continue
        for field in (
            "model_attempts",
            "tool_calls",
            "iterations",
            "replans",
            "supplements",
            "quality_repairs",
            "input_tokens",
            "output_tokens",
            "elapsed_ms",
        ):
            setattr(
                consumed,
                field,
                getattr(consumed, field) + getattr(entry.consumption, field),
            )
    receipt = proto.SubmittedDraftReceipt(
        draft_key=first.draft_key,
        content_hash=str(HashDigest.of_bytes(first.draft_patch)),
        task_version=4,
        content=first.draft_patch,
    )

    class ForbiddenModel:
        def complete(self, *_):
            raise AssertionError("model must not be called")

    capability_calls = 0

    def forbidden_capability(_):
        nonlocal capability_calls
        capability_calls += 1
        raise AssertionError("capability must not be created")

    resumed = ValidatedAgentRuntime(
        ResumeValidator(CheckpointCodec()),
        LangGraphAgentLoop(
            model=ForbiddenModel(),
            checkpoint_codec=CheckpointCodec(),
            quality_policy=DraftQualityPolicy(),
            required_coverage=(),
            capability_factory=forbidden_capability,
        ),
    )(
        _context(
            checkpoint=first_checkpoint,
            checkpoint_sequence=first_checkpoint_sequence,
            repository_binding_id="",
            repository_revision="",
            submitted_draft=receipt,
            execution_ledger_version="run-ledger.v1",
            run_budget=proto.RunBudget(
                max_model_attempts=4,
                max_tool_calls=2,
                max_iterations=3,
                max_replans=1,
                max_supplements=1,
                max_quality_repairs=1,
                max_input_tokens=20_000,
                max_output_tokens=4_000,
                max_elapsed_ms=60_000,
            ),
            consumed_budget=consumed,
            ledger_entries=ledger_entries,
            resume_artifacts=tuple(first_sink.artifacts),
            resume_summary=proto.ResumeStateSummary(
                checkpoint_content_hash=str(HashDigest.of_bytes(first_checkpoint)),
                terminal_model_attempt_count=2,
                artifact_count=len(first_sink.artifacts),
                artifacts=(
                    proto.RunArtifactIdentity(
                        artifact_key=item.artifact_key,
                        artifact_type=item.artifact_type,
                        generation=item.generation,
                        request_hash=item.request_hash,
                        content_hash=item.content_hash,
                    )
                    for item in first_sink.artifacts
                ),
            ),
        )
    )
    assert resumed.submission_disposition is SubmissionDisposition.TERMINAL_ACK_ONLY
    assert resumed.draft_key is None
    assert capability_calls == 0
