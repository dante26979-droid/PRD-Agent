from agent.information_need import (
    InformationNeedPolicy,
    NeedKind,
    NeedPlanningContext,
    NeedRoute,
    PlannedNeedDraft,
    Requiredness,
    SourceType,
)


def test_new_behavior_without_current_state_dependency_skips_investigation():
    decision = InformationNeedPolicy().apply(
        NeedPlanningContext(
            run_id="run-new",
            task_id="task-new",
            task_message="新增一个独立的欢迎页，不依赖当前系统实现",
            task_version=1,
            workflow_version="agent-runtime.v4",
            repository_binding_id="",
            repository_revision="",
            historical_prd_available=False,
            remaining_model_attempts=3,
            remaining_tool_calls=2,
            remaining_iterations=3,
            remaining_replans=1,
        ),
        PlannedNeedDraft(
            question="是否存在需要调查的当前实现？",
            suggested_requiredness=Requiredness.NONE,
            need_kind=NeedKind.NEW_BEHAVIOR,
            source_types=(),
            fallback="按目标态生成，并将当前态标记为不适用",
        ),
    )

    assert decision.plan.effective_requiredness is Requiredness.NONE
    assert decision.plan.required_coverage == ()
    assert decision.route is NeedRoute.SKIP_INVESTIGATION
    assert decision.reason_code == "NONE_NOT_REQUIRED"


def test_api_field_change_cannot_be_downgraded_by_the_planner():
    decision = InformationNeedPolicy().apply(
        NeedPlanningContext(
            run_id="run-api",
            task_id="task-api",
            task_message="修改订单查询 API，新增 created_at 筛选字段",
            task_version=1,
            workflow_version="agent-runtime.v4",
            repository_binding_id="binding-1",
            repository_revision="a" * 40,
            historical_prd_available=False,
            remaining_model_attempts=3,
            remaining_tool_calls=3,
            remaining_iterations=3,
            remaining_replans=1,
        ),
        PlannedNeedDraft(
            question="当前订单查询接口和字段校验如何实现？",
            suggested_requiredness=Requiredness.NONE,
            need_kind=NeedKind.FIELD_OR_FORMAT_CHANGE,
            source_types=(),
            fallback="无法确认的当前态标记为 Unknown",
        ),
    )

    assert decision.plan.effective_requiredness is Requiredness.REQUIRED
    assert decision.plan.source_types == (SourceType.CODE,)
    assert tuple(item.key for item in decision.plan.required_coverage) == (
        "api_contract",
        "validation_logic",
        "storage_schema",
        "tests",
    )
    assert all(item.blocking for item in decision.plan.required_coverage)
    assert decision.route is NeedRoute.EXECUTE_INVESTIGATION
    assert decision.reason_code == "REQUIRED_SOURCE_AVAILABLE"


def test_optional_historical_context_records_why_it_was_skipped():
    decision = InformationNeedPolicy().apply(
        NeedPlanningContext(
            run_id="run-history",
            task_id="task-history",
            task_message="补充订单术语的历史背景",
            task_version=1,
            workflow_version="agent-runtime.v4",
            repository_binding_id="",
            repository_revision="",
            historical_prd_available=False,
            remaining_model_attempts=3,
            remaining_tool_calls=3,
            remaining_iterations=3,
            remaining_replans=1,
        ),
        PlannedNeedDraft(
            question="历史 PRD 如何定义订单术语？",
            suggested_requiredness=Requiredness.OPTIONAL,
            need_kind=NeedKind.HISTORICAL_PRD_CONTEXT,
            source_types=(SourceType.HISTORICAL_PRD,),
            fallback="不引用无法访问的历史规则",
        ),
    )

    assert decision.plan.effective_requiredness is Requiredness.OPTIONAL
    assert decision.route is NeedRoute.SKIP_INVESTIGATION
    assert decision.reason_code == "OPTIONAL_SKIPPED_SOURCE_UNAVAILABLE"
