from __future__ import annotations

from agent.capability.client import (
    ProjectMemoryConflict,
    ProjectMemoryItem,
    ProjectMemorySearchResult,
)
from agent.context import RunContext
from agent.project_memory import ProjectMemoryModule, ProjectMemoryPolicy
from agent.runtime import BufferedRuntimeEventSink, RunExecutionLedger
from agent.v1 import agent_execution_pb2 as proto


class MemoryGateway:
    def __init__(self) -> None:
        self.calls = 0

    def search_project_memory(self, **_kwargs):
        self.calls += 1
        return ProjectMemorySearchResult(
            space_id="space-1",
            memory_watermark=7,
            records=(
                ProjectMemoryItem(
                    memory_id="memory-1",
                    version=2,
                    memory_type="PROJECT_DECISION",
                    subject="runtime",
                    predicate="control_plane",
                    value="go",
                    statement="The control plane is implemented in Go.",
                    authority_class="USER_CONFIRMED",
                    tags=("architecture",),
                    sensitivity="INTERNAL",
                    source_refs=(),
                    committed_epoch=6,
                    content_hash="sha256:memory",
                ),
            ),
            conflicts=(
                ProjectMemoryConflict(
                    conflict_id="conflict-1",
                    subject="runtime",
                    predicate="language",
                    memory_ids=("memory-1", "memory-2"),
                ),
            ),
            excluded_count=1,
            source_set_hash="sha256:source-set",
        )


def _runtime():
    sink = BufferedRuntimeEventSink()
    context = RunContext(
        run_id="run-1",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-1",
        task_message="Design the Agent runtime",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=8,
            max_tool_calls=8,
            max_iterations=8,
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            max_elapsed_ms=100_000,
        ),
        consumed_budget=proto.ConsumedBudget(),
        event_sink=sink,
        memory_space_id="space-1",
        memory_watermark=7,
        memory_policy_version="project-memory-policy.v1",
        memory_access_scope_hash="sha256:access",
        memory_assignment_hash="sha256:assignment",
    )
    return context, sink, RunExecutionLedger(context)


def test_memory_bundle_is_ledger_owned_injected_and_replayed():
    context, sink, ledger = _runtime()
    gateway = MemoryGateway()
    module = ProjectMemoryModule(
        ProjectMemoryPolicy(version="project-memory-policy.v1", mode="enforce")
    )

    first = module.prepare(
        context=context,
        gateway=gateway,
        ledger=ledger,
        operation="plan_outline",
        operation_sequence=1,
        payload={"task_message": context.task_message},
    )
    second = module.prepare(
        context=context,
        gateway=gateway,
        ledger=ledger,
        operation="plan_outline",
        operation_sequence=1,
        payload={"task_message": context.task_message},
    )

    assert gateway.calls == 1
    assert first.payload["project_memory"]["memory_watermark"] == 7
    assert first.payload["project_memory"]["records"][0]["memory_id"] == "memory-1"
    assert first.bundle_artifact_key
    assert second.replayed is True
    assert second.bundle_artifact_hash == first.bundle_artifact_hash
    assert len(ledger.entries()) == 2
    assert {item.entry_kind for item in ledger.entries()} == {
        "CAPABILITY",
        "LOCAL_DERIVATION",
    }


def test_shadow_memory_records_artifacts_without_changing_model_payload():
    context, _sink, ledger = _runtime()
    gateway = MemoryGateway()
    payload = {"task_message": context.task_message}
    prepared = ProjectMemoryModule(
        ProjectMemoryPolicy(version="project-memory-policy.v1", mode="shadow")
    ).prepare(
        context=context,
        gateway=gateway,
        ledger=ledger,
        operation="plan_outline",
        operation_sequence=1,
        payload=payload,
    )

    assert prepared.payload == payload
    assert prepared.bundle is not None
    assert gateway.calls == 1
