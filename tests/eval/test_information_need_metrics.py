from datetime import datetime, timezone

from prd_agent.eval.metrics import evaluate_information_need
from prd_agent.eval.models import BaselineConfig, BaselineRun, EvalCase, EvalDataset
from prd_agent.eval.report import build_report


def _case(expected_type):
    return EvalCase.from_dict(
        {
            "case_id": "case-need",
            "title": "Need decision",
            "requirement": "新增独立欢迎页",
            "context": "不依赖当前实现",
            "repository_id": "demo",
            "resolved_commit_sha": "a" * 40,
            "expected_information_needs": [
                {
                    "type": expected_type,
                    "category": "validation_logic",
                    "reason": "不需要当前实现事实",
                }
            ],
            "required_prd_sections": ["requirement_summary"],
        }
    )


def test_not_required_fixture_is_normalized_to_none_for_need_metrics():
    case = _case("NOT_REQUIRED")

    metrics = {
        item.name: item
        for item in evaluate_information_need(
            case,
            {
                "information_need": {
                    "requiredness": "NONE",
                    "route": "SKIP_INVESTIGATION",
                    "reason_code": "NONE_NOT_REQUIRED",
                },
                "counters": {"capability_physical_call_count": 0},
            },
        )
    }

    assert case.expected_information_needs[0].kind == "NONE"
    assert metrics["information_need_accuracy"].value == 1.0
    assert metrics["none_capability_call_rate"].value == 0.0


def test_report_contains_requiredness_confusion_matrix():
    case = _case("REQUIRED")
    dataset = EvalDataset("eval-v1", "demo", "a" * 40, (case,))
    config = BaselineConfig("v4", "prompt", "model", "eval-v1", 1)
    trace = {
        "schema_version": "agent-loop-trace.v1",
        "eval_run_id": "run-1",
        "case_id": case.case_id,
        "workflow_version": "agent-runtime.v4",
        "execution_mode": "scripted",
        "deterministic_only": True,
        "status": "completed",
        "started_at": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
        "duration_ms": 1,
        "input_hash": "sha256:a",
        "output_hash": "sha256:b",
        "result_outcome": "EMPTY_EVIDENCE",
        "stop_reason": "COVERAGE_COMPLETE",
        "information_need": {
            "requiredness": "OPTIONAL",
            "route": "SKIP_INVESTIGATION",
            "reason_code": "OPTIONAL_SKIPPED_BUDGET",
        },
        "counters": {
            "capability_physical_call_count": 0,
        },
    }
    run = BaselineRun(
        eval_run_id="run-1",
        case_id=case.case_id,
        config_id="v4",
        trial_no=1,
        model_id="model",
        prompt_version="prompt",
        dataset_version="eval-v1",
        repository_commit="a" * 40,
        input_hash="sha256:a",
        output_hash="sha256:b",
        started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        duration_ms=1,
        token_usage={},
        status="completed",
        output="# 需求摘要\n内容",
        metadata={"trace": trace},
    )

    report = build_report(dataset, config, [run])

    assert report.information_need_confusion_matrix["REQUIRED"]["OPTIONAL"] == 1
    assert report.metric_summary["information_need_required_recall"]["mean"] == 0.0
