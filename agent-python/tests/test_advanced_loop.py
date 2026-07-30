from __future__ import annotations

import json
import hashlib

import pytest

from agent.checkpoint import CheckpointCodec
from agent.capability import RepositorySearchHit
from agent.context import RunContext
from agent.graph import LangGraphAgentLoop
from agent.graph.advanced import AdvancedLoopRunner
from agent.graph.snapshot import LoopCheckpointStatus
from agent.draft import DraftBundle, DraftUnit
from agent.model import ModelResponse
from agent.quality import DraftQualityPolicy
from agent.runtime import BufferedRuntimeEventSink
from agent.v1 import agent_execution_pb2 as proto


class DraftModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, _system: str, _user: str) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            output=json.dumps(
                {
                    "markdown": (
                        "# PRD\n\n"
                        "## 功能范围\n\n生成结构化草稿。\n\n"
                        "## 验收标准\n\n快照重启后不重复生成草稿。"
                    )
                },
                ensure_ascii=False,
            ),
            token_usage={"total_tokens": 8},
            model_id="test",
        )


class FailModel:
    def complete(self, _system: str, _user: str) -> ModelResponse:
        raise AssertionError("resume must use the durable draft artifact")


class StopAfterDraft(BufferedRuntimeEventSink):
    def checkpoint(self, sequence: int, payload: bytes) -> None:
        super().checkpoint(sequence, payload)
        decoded = CheckpointCodec().decode(payload)
        if decoded.payload["status"] == "DRAFTED":
            raise RuntimeError("simulated worker restart")


def _context(*, sink, checkpoint=b"", sequence=0, artifacts=()):
    return RunContext(
        run_id="run-advanced",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-advanced",
        task_message="生成一份支持独立确认的 PRD",
        task_version=1,
        workflow_version="agent-runtime.v1",
        checkpoint=checkpoint,
        checkpoint_sequence=sequence,
        resume_artifacts=artifacts,
        event_sink=sink,
    )


def _loop(model) -> LangGraphAgentLoop:
    return LangGraphAgentLoop(
        model=model,
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        required_coverage=(),
        advanced_loop_mode="enforce",
        max_supplements=0,
    )


def test_advanced_loop_persists_artifacts_and_builds_confirmation_units():
    sink = BufferedRuntimeEventSink()

    result = _loop(DraftModel())(_context(sink=sink))
    draft = json.loads(result.draft_patch)
    statuses = [
        CheckpointCodec().decode(payload).payload["status"]
        for _, payload in sink.checkpoints
    ]

    assert draft["schema_version"] == "working-draft.v2"
    assert draft["grounding_outcome"] == "GROUNDED"
    assert draft["quality_outcome"] == "QUALITY_PASSED"
    assert len(draft["confirmation_units"]) >= 2
    assert {item.artifact_type for item in sink.artifacts} == {
        "DRAFT_BUNDLE",
        "GROUNDING_REPORT",
        "QUALITY_REPORT",
        "CONFIRMATION_UNITS",
    }
    assert statuses[-5:] == [
        "DRAFTED",
        "GROUNDED",
        "QUALITY_PASSED",
        "CONFIRMATION_UNITS_BUILT",
        "READY_TO_SUBMIT",
    ]


def test_resume_from_drafted_status_uses_artifact_instead_of_model():
    first_sink = StopAfterDraft()
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        _loop(DraftModel())(_context(sink=first_sink))

    sequence, checkpoint = first_sink.checkpoints[-1]
    resume_sink = BufferedRuntimeEventSink()
    result = _loop(FailModel())(
        _context(
            sink=resume_sink,
            checkpoint=checkpoint,
            sequence=sequence,
            artifacts=tuple(first_sink.artifacts),
        )
    )

    draft = json.loads(result.draft_patch)
    assert draft["quality_outcome"] == "QUALITY_PASSED"
    assert draft["confirmation_units"]
    assert CheckpointCodec().decode(resume_sink.checkpoints[0][1]).payload[
        "status"
    ] == "GROUNDED"


def test_scoped_revision_preserves_confirmed_unit_content():
    scope_markdown = "## 范围\n\n原始且已确认的范围。"
    acceptance_markdown = "## 验收\n\n原始验收标准。"
    base = json.dumps(
        {
            "schema_version": "working-draft.v2",
            "confirmation_units": [
                {
                    "unit_key": "scope",
                    "title": "范围",
                    "order": 10,
                    "markdown": scope_markdown,
                    "depends_on": [],
                },
                {
                    "unit_key": "acceptance",
                    "title": "验收",
                    "order": 20,
                    "markdown": acceptance_markdown,
                    "depends_on": ["scope"],
                },
            ],
        },
        ensure_ascii=False,
    ).encode()
    sink = BufferedRuntimeEventSink()
    context = RunContext(
        run_id="run-revision",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-advanced",
        task_message="修订验收标准",
        task_version=5,
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        revision_scope=proto.RevisionScope(
            base_draft_id="draft-base",
            base_draft_hash="base-hash",
            reopened_unit_keys=["acceptance"],
            immutable_unit_keys=["scope"],
            user_feedback="补充异常场景",
        ),
        resume_draft=proto.SubmittedDraftReceipt(
            draft_key="draft-base-key",
            content_hash="base-hash",
            task_version=4,
            content=base,
        ),
        event_sink=sink,
    )

    result = _loop(DraftModel())(context)
    units = {
        item["unit_key"]: item
        for item in json.loads(result.draft_patch)["confirmation_units"]
    }

    assert units["scope"]["markdown"] == scope_markdown
    assert units["scope"]["immutable"] is True
    assert "补充异常场景" in units["acceptance"]["markdown"]
    assert units["acceptance"]["immutable"] is False


def test_unsupported_current_state_triggers_one_targeted_supplement():
    class StructuredModel:
        def complete(self, _system, _user):
            return ModelResponse(
                output=json.dumps(
                    {
                        "markdown": "## 当前实现\n\n订单路由已经存在。",
                        "units": [
                            {
                                "unit_key": "current",
                                "title": "当前实现",
                                "order": 10,
                                "markdown": "## 当前实现\n\n订单路由已经存在。",
                            }
                        ],
                        "claims": [
                            {
                                "unit_key": "current",
                                "claim_type": "CURRENT_STATE",
                                "criticality": "BLOCKING",
                                "statement": "order route",
                                "evidence_refs": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                token_usage={"total_tokens": 5},
                model_id="structured-test",
            )

    class Gateway:
        def __init__(self):
            self.queries = []

        def search_repository(self, **kwargs):
            self.queries.append(kwargs["query"])
            return (
                RepositorySearchHit(
                    path="api/orders.py",
                    line=10,
                    snippet="def order_route(): ...",
                ),
            )

        def close(self):
            pass

    gateway = Gateway()
    sink = BufferedRuntimeEventSink()
    context = RunContext(
        run_id="run-grounding",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-grounding",
        task_message="梳理订单路由现状",
        task_version=1,
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        repository_binding_id="binding-1",
        repository_revision="a" * 40,
        event_sink=sink,
    )
    loop = LangGraphAgentLoop(
        model=StructuredModel(),
        checkpoint_codec=CheckpointCodec(),
        quality_policy=DraftQualityPolicy(),
        capability_factory=lambda _context: gateway,
        required_coverage=(),
        advanced_loop_mode="enforce",
        max_supplements=1,
    )

    result = loop(context)
    draft = json.loads(result.draft_patch)
    statuses = [
        CheckpointCodec().decode(payload).payload["status"]
        for _, payload in sink.checkpoints
    ]

    assert gateway.queries == ["order route"]
    assert draft["grounding_outcome"] == "GROUNDED"
    assert draft["evidence_refs"]
    assert statuses.count("GROUNDING_SUPPLEMENT_REQUIRED") == 1
    assert statuses.count("DRAFTED") == 2


def test_quality_repair_reenters_draft_and_grounding_once():
    broken = DraftBundle(
        schema_version="draft-bundle.v1",
        generation=1,
        units=(DraftUnit("scope", "范围", "", 10),),
        claims=(),
        unknowns=(),
        markdown="",
    )
    content = json.dumps(
        broken.as_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(content).hexdigest()
    artifact = proto.RunArtifact(
        artifact_key="run-repair:draft_bundle:1",
        artifact_type="DRAFT_BUNDLE",
        generation=1,
        request_hash="request",
        content_hash=digest,
        content=content,
    )
    sink = BufferedRuntimeEventSink()
    context = RunContext(
        run_id="run-repair",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-repair",
        task_message="修复空单元",
        task_version=1,
        workflow_version="agent-runtime.v1",
        checkpoint=b"",
        resume_artifacts=(artifact,),
        event_sink=sink,
    )
    statuses = []
    repairs = []

    def checkpoint(_state, status):
        statuses.append(status.value)
        return len(statuses)

    def repair(_state, issues):
        repairs.append(issues)
        return "## 范围\n\n修复后的有效内容。", 3

    state = AdvancedLoopRunner(
        context=context,
        sink=sink,
        emit_checkpoint=checkpoint,
        generate_draft=lambda _state: (_ for _ in ()).throw(
            AssertionError("draft generation is not expected")
        ),
        repair_draft=repair,
        supplement=None,
        max_repairs=1,
    ).invoke(
        {
            "status": LoopCheckpointStatus.GROUNDED.value,
            "draft_generation": 1,
            "draft_artifact_key": artifact.artifact_key,
            "draft_artifact_hash": digest,
            "grounding_outcome": "GROUNDED",
            "repair_count": 0,
            "checkpoint_sequence": 0,
        }
    )

    assert len(repairs) == 1
    assert statuses == [
        "QUALITY_REPAIR_REQUIRED",
        "DRAFTED",
        "GROUNDED",
        "QUALITY_PASSED",
        "CONFIRMATION_UNITS_BUILT",
    ]
    assert state["repair_count"] == 1
    assert state["quality_outcome"] == "QUALITY_PASSED"
