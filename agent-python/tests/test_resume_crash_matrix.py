from __future__ import annotations

import pytest
import hashlib

from agent.checkpoint import CheckpointCodec
from agent.context import RunContext
from agent.graph.snapshot import LoopCheckpointStatus, LoopSnapshot
from agent.resume.validator import ResumeValidator
from agent.v1 import agent_execution_pb2 as proto


@pytest.mark.parametrize("status", tuple(LoopCheckpointStatus))
def test_every_checkpoint_status_has_a_strict_v4_resume_route(status):
    state = {
        "run_id": "run-1",
        "task_id": "task-1",
        "task_version": 1,
        "workflow_version": "agent-runtime.v4",
        "repository_binding_id": "",
        "repository_revision": "",
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
    artifacts = ()
    if status is LoopCheckpointStatus.NEED_PLANNED:
        content = b"need-plan"
        content_hash = hashlib.sha256(content).hexdigest()
        request_hash = "sha256:" + "b" * 64
        artifact = proto.RunArtifact(
            artifact_key="run-1:information_need:plan:1",
            artifact_type="INFORMATION_NEED_PLAN",
            generation=1,
            request_hash=request_hash,
            content_hash=content_hash,
            content=content,
        )
        artifacts = (artifact,)
        state.update(
            information_need_plan_id="need-1",
            information_need_context_hash="sha256:" + "c" * 64,
            information_need_artifact_key=artifact.artifact_key,
            information_need_artifact_hash=content_hash,
            information_need_artifact_type=artifact.artifact_type,
            information_need_artifact_generation=1,
            information_need_artifact_request_hash=request_hash,
            effective_requiredness="NONE",
            need_route="SKIP_INVESTIGATION",
            need_route_reason_code="NONE_NOT_REQUIRED",
        )
    if status is LoopCheckpointStatus.ACTION_VALIDATED:
        state.update(pending_action={"kind": "search"}, action_signature="sig-1")
    if status is LoopCheckpointStatus.READY_TO_SUBMIT:
        state.update(
            candidate_markdown="# PRD",
            draft_hash="sha256:" + "a" * 64,
            submission={
                "draft_key": "run-1:draft:1",
                "draft_patch_hash": "sha256:" + "b" * 64,
                "markdown_hash": "sha256:" + "a" * 64,
                "expected_task_version": 1,
            },
        )
    payload = LoopSnapshot(status, state).as_payload(
        workflow_version="agent-runtime.v4"
    )
    checkpoint = CheckpointCodec().encode(
        workflow_version="agent-runtime.v4",
        run_id="run-1",
        task_version=1,
        sequence=1,
        payload=payload,
    )
    validated = ResumeValidator(CheckpointCodec()).hydrate(
        RunContext(
            run_id="run-1",
            tenant_id="tenant-1",
            owner_id="owner-1",
            task_id="task-1",
            task_message="write a PRD",
            workflow_version="agent-runtime.v4",
            checkpoint=checkpoint,
            checkpoint_sequence=1,
            task_version=1,
            resume_summary=proto.ResumeStateSummary(
                checkpoint_content_hash="sha256:"
                + hashlib.sha256(checkpoint).hexdigest(),
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
            ),
            resume_artifacts=artifacts,
        )
    )
    assert validated.status is status
