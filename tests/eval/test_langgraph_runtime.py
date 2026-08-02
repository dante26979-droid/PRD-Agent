from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from prd_agent.eval.agent_trace import AgentLoopTrace
from prd_agent.eval.case_loader import load_dataset
from prd_agent.eval.cli import _load_config, _select_baseline
from prd_agent.eval.langgraph_runtime import (
    AgentRuntimeUnavailable,
    LangGraphRuntimeBaseline,
    _agent_dependencies,
)
from prd_agent.eval.models import AgentEvalMetadata, BaselineConfig
from prd_agent.eval.runner import BaselineRunner, InMemoryRunStore


ROOT = Path(__file__).resolve().parents[2]


def _dataset():
    return load_dataset(ROOT / "eval/cases/manifest.json")


def _config():
    return _load_config(ROOT / "eval/configs/langgraph_v1_characterization.json")


def _baseline():
    return LangGraphRuntimeBaseline(
        ROOT, ROOT / "eval/fixtures/langgraph_v1_actions.json"
    )


def test_input_hash_and_run_id_are_stable_and_trial_is_separate():
    case = _dataset().cases[0]
    config = _config()
    baseline = _baseline()

    assert baseline.input_hash(case, config) == baseline.input_hash(case, config)
    assert baseline.run_id(case, config, 1) == baseline.run_id(case, config, 1)
    assert baseline.run_id(case, config, 1) != baseline.run_id(case, config, 2)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("max_iterations", 99),
        ("workflow_version", "agent-runtime.v2"),
        ("token_budget", 999),
    ],
)
def test_config_changes_input_hash(option, value):
    case = _dataset().cases[0]
    config = _config()
    changed = BaselineConfig(
        config.config_id,
        config.prompt_version,
        config.model_id,
        config.dataset_version,
        config.trials_per_case,
        config.timeout_seconds,
        {**config.options, option: value},
    )

    assert _baseline().input_hash(case, config) != _baseline().input_hash(case, changed)


def test_direct_case_runs_production_loop_without_capability():
    case = _dataset().cases[0]
    run = _baseline().run_case(case, _config(), None, trial_no=1)
    trace = AgentLoopTrace.from_dict(run.metadata["trace"])

    assert run.status == "completed"
    assert run.metadata["route"] == "LEGACY_DIRECT_GENERATION"
    assert trace.counters.capability_physical_call_count == 0
    assert trace.counters.coverage_item_count == 0


def test_required_case_records_action_observation_and_ready_checkpoint():
    case = next(item for item in _dataset().cases if item.case_id == "case-006")
    run = _baseline().run_case(case, _config(), None, trial_no=1)
    trace = AgentLoopTrace.from_dict(run.metadata["trace"])

    assert run.status == "completed"
    assert trace.counters.capability_physical_call_count == 1
    assert trace.capability_calls[0].target_coverage == ("repository_evidence",)
    assert [item.status for item in trace.checkpoints] == [
        "ACTION_VALIDATED",
        "OBSERVED",
        "INVESTIGATION_FINISHED",
        "READY_TO_SUBMIT",
    ]


def test_cross_file_case_records_two_distinct_actions():
    case = next(item for item in _dataset().cases if item.case_id == "case-010")
    run = _baseline().run_case(case, _config(), None, trial_no=1)
    trace = AgentLoopTrace.from_dict(run.metadata["trace"])

    assert trace.counters.capability_physical_call_count == 2
    assert trace.counters.coverage_item_count == 2
    assert trace.counters.coverage_covered_count == 2
    assert len({item.action_signature for item in trace.capability_calls}) == 2


def test_invalid_model_json_becomes_a_classified_failed_run(monkeypatch):
    baseline = _baseline()
    case = _dataset().cases[0]
    monkeypatch.setattr(
        baseline,
        "_outputs",
        lambda *_args: [{"_invalid_json": "{"}],
    )

    run = baseline.run_case(case, _config(), None, trial_no=1)
    trace = AgentLoopTrace.from_dict(run.metadata["trace"])

    assert run.status == "failed"
    assert run.error == "MODEL_INVALID_OUTPUT"
    assert trace.failure.category == "MODEL_INVALID_OUTPUT"
    assert trace.model_attempts[0].status == "SUCCEEDED"


def test_all_fixed_cases_produce_a_valid_trace():
    dataset = _dataset()
    config = _config()
    runs = BaselineRunner(
        dataset, config, object(), InMemoryRunStore(), baseline=_baseline()
    ).run()

    assert len(runs) == 10
    assert all(run.status == "completed" for run in runs)
    assert all(
        AgentLoopTrace.from_dict(run.metadata["trace"]).schema_version
        == "agent-loop-trace.v1"
        for run in runs
    )


def test_completed_run_is_replayed_without_executing_baseline_again(monkeypatch):
    dataset = _dataset()
    config = _config()
    baseline = _baseline()
    store = InMemoryRunStore()
    runner = BaselineRunner(dataset, config, object(), store, baseline=baseline)
    first = runner.run()

    monkeypatch.setattr(
        baseline,
        "run_case",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must replay")),
    )

    assert runner.run() == first


def test_agent_metadata_rejects_non_json_safe_values():
    with pytest.raises(ValueError, match="JSON-safe"):
        AgentEvalMetadata(
            "scripted_characterization",
            True,
            "agent-runtime.v1",
            "LEGACY_DIRECT_GENERATION",
            {"unsafe": {"set"}},
        )


def test_missing_agent_package_has_an_actionable_error(monkeypatch):
    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "agent.checkpoint":
            raise ImportError("blocked for test")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(AgentRuntimeUnavailable, match="agent-python on PYTHONPATH"):
        _agent_dependencies()


@pytest.mark.parametrize(
    ("config_id", "expected_name"),
    [
        ("direct-prompt-v1", "DirectPromptBaseline"),
        ("minimal-workflow-v1", "MinimalWorkflowBaseline"),
        ("single-retrieval-v1", "SingleRetrievalBaseline"),
        ("bounded-investigation-v1", "BoundedInvestigationBaseline"),
        ("langgraph-runtime-v1-characterization", "LangGraphRuntimeBaseline"),
    ],
)
def test_cli_baseline_routing_preserves_legacy_variants(config_id, expected_name):
    config = BaselineConfig(config_id, "v1", "model", "eval-v1", 1)

    _model, baseline = _select_baseline(config, ROOT)

    assert type(baseline).__name__ == expected_name
