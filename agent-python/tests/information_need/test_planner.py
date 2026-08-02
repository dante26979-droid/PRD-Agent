from dataclasses import replace

from agent.context import RunContext
from agent.checkpoint import CheckpointCodec
from agent.graph.snapshot import LoopCheckpointStatus
from agent.information_need import (
    InformationNeedPlanner,
    NeedPlanningContext,
    NeedRoute,
    Requiredness,
)
from agent.runtime import BufferedRuntimeEventSink, RunExecutionLedger
from agent.resume.validator import ResumeValidator
from agent.testing import ScriptedAgentModel
from agent.v1 import agent_execution_pb2 as proto


def _runtime_context(sink):
    return RunContext(
        run_id="run-plan",
        tenant_id="tenant",
        owner_id="owner",
        task_id="task-plan",
        task_message="修改订单查询 API，新增 created_at 筛选字段",
        workflow_version="agent-runtime.v4",
        checkpoint=b"",
        repository_binding_id="binding-1",
        repository_revision="a" * 40,
        execution_ledger_version="run-ledger.v1",
        run_budget=proto.RunBudget(
            max_model_attempts=4,
            max_tool_calls=3,
            max_iterations=3,
            max_replans=1,
            max_input_tokens=20_000,
            max_output_tokens=4_000,
            max_elapsed_ms=60_000,
        ),
        consumed_budget=proto.ConsumedBudget(),
        event_sink=sink,
    )


def _planning_context():
    return NeedPlanningContext(
        run_id="run-plan",
        task_id="task-plan",
        task_message="修改订单查询 API，新增 created_at 筛选字段",
        task_version=1,
        workflow_version="agent-runtime.v4",
        repository_binding_id="binding-1",
        repository_revision="a" * 40,
        historical_prd_available=False,
        remaining_model_attempts=4,
        remaining_tool_calls=3,
        remaining_iterations=3,
        remaining_replans=1,
    )


def test_planner_persists_a_policy_applied_plan_after_the_ledger_outcome():
    sink = BufferedRuntimeEventSink()
    model = ScriptedAgentModel(
        [
            {
                "question": "当前订单查询接口和字段校验如何实现？",
                "suggested_requiredness": "NONE",
                "need_kind": "FIELD_OR_FORMAT_CHANGE",
                "source_types": [],
                "fallback": "无法确认的当前态标记为 Unknown",
            }
        ]
    )
    ledger = RunExecutionLedger(_runtime_context(sink))

    decision = InformationNeedPlanner(model, ledger, sink).plan(_planning_context())

    assert decision.plan.effective_requiredness is Requiredness.REQUIRED
    assert decision.route is NeedRoute.EXECUTE_INVESTIGATION
    assert [item.artifact_type for item in sink.artifacts] == [
        "MODEL_VALIDATED_OUTPUT",
        "INFORMATION_NEED_PLAN",
    ]
    assert [event.event_type for event in sink.ledger_events] == [
        "LEDGER_RESERVED",
        "LEDGER_CALL_STARTED",
        "LEDGER_FINISHED",
    ]
    assert len(model.calls) == 1


def test_durable_plan_recovery_does_not_call_the_model_again():
    first_sink = BufferedRuntimeEventSink()
    first_model = ScriptedAgentModel(
        [
            {
                "question": "当前订单查询接口和字段校验如何实现？",
                "suggested_requiredness": "REQUIRED",
                "need_kind": "FIELD_OR_FORMAT_CHANGE",
                "source_types": ["CODE"],
                "fallback": "无法确认的当前态标记为 Unknown",
            }
        ]
    )
    first_runtime = _runtime_context(first_sink)
    InformationNeedPlanner(
        first_model, RunExecutionLedger(first_runtime), first_sink
    ).plan(_planning_context())
    terminal = first_sink.ledger_events[-1].entry

    resumed_sink = BufferedRuntimeEventSink()
    resumed_runtime = replace(
        first_runtime,
        consumed_budget=proto.ConsumedBudget().FromString(
            terminal.consumption.SerializeToString()
        ),
        ledger_entries=(terminal,),
        resume_artifacts=tuple(first_sink.artifacts),
        event_sink=resumed_sink,
    )
    resumed_model = ScriptedAgentModel([])

    decision = InformationNeedPlanner(
        resumed_model,
        RunExecutionLedger(resumed_runtime),
        resumed_sink,
        resume_artifacts=resumed_runtime.resume_artifacts,
    ).plan(_planning_context())

    assert decision.replayed is True
    assert decision.plan.effective_requiredness is Requiredness.REQUIRED
    assert resumed_model.calls == []
    assert resumed_sink.ledger_events == []
    assert resumed_sink.artifacts == []


def test_resume_validator_accepts_a_ledger_owned_plan_ahead_of_checkpoint():
    sink = BufferedRuntimeEventSink()
    runtime = _runtime_context(sink)
    InformationNeedPlanner(
        ScriptedAgentModel(
            [
                {
                    "question": "当前订单查询接口如何实现？",
                    "suggested_requiredness": "REQUIRED",
                    "need_kind": "FIELD_OR_FORMAT_CHANGE",
                    "source_types": ["CODE"],
                    "fallback": "当前态标记为 Unknown",
                }
            ]
        ),
        RunExecutionLedger(runtime),
        sink,
    ).plan(_planning_context())
    terminal = sink.ledger_events[-1].entry
    resumed = replace(
        runtime,
        event_sink=None,
        consumed_budget=proto.ConsumedBudget().FromString(
            terminal.consumption.SerializeToString()
        ),
        ledger_entries=(terminal,),
        resume_artifacts=tuple(sink.artifacts),
    )

    validated = ResumeValidator(CheckpointCodec()).hydrate(resumed)

    assert validated.state is None
