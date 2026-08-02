from __future__ import annotations

from datetime import datetime, timezone
import json

from prd_agent.eval.models import BaselineConfig, BaselineRun, EvalCase, EvalDataset
from prd_agent.eval.report import build_report


def _dataset():
    case = EvalCase.from_dict(
        {
            "case_id": "case-redaction",
            "title": "Redaction",
            "requirement": "safe report",
            "context": "fixed",
            "repository_id": "demo",
            "resolved_commit_sha": "a" * 40,
            "required_prd_sections": ["requirement_summary"],
        }
    )
    return EvalDataset("eval-v1", "demo", "a" * 40, (case,))


def _run(*, status="completed", output="DRAFT_SECRET_MARKER_9d2f", error=None):
    return BaselineRun(
        eval_run_id="run-redaction",
        case_id="case-redaction",
        config_id="langgraph-runtime-v1-characterization",
        trial_no=1,
        model_id="scripted-agent-model",
        prompt_version="prompt-version-safe",
        dataset_version="eval-v1",
        repository_commit="a" * 40,
        input_hash="sha256:input",
        output_hash="sha256:output" if output else None,
        started_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        duration_ms=4,
        token_usage={},
        status=status,
        output=output,
        error=error,
        metadata={
            "measurement_mode": "scripted_characterization",
            "deterministic_only": True,
            "workflow_version": "agent-runtime.v1",
            "route": "LEGACY_DIRECT_GENERATION",
            "trace": {"counters": {"model_attempt_count": 1}},
        },
    )


def test_json_and_markdown_do_not_contain_draft_or_runtime_metadata_content():
    run = _run()
    run.metadata["prompt"] = "PROMPT_SECRET_MARKER_b819"
    run.metadata["excerpt"] = "EVIDENCE_SECRET_MARKER_31aa"
    report = build_report(
        _dataset(),
        BaselineConfig("safe", "v1", "scripted", "eval-v1", 1),
        [run],
    )

    rendered = report.to_json() + report.to_markdown()

    for marker in (
        "DRAFT_SECRET_MARKER_9d2f",
        "PROMPT_SECRET_MARKER_b819",
        "EVIDENCE_SECRET_MARKER_31aa",
    ):
        assert marker not in rendered
    assert "scripted_characterization" in rendered
    assert '"deterministic_only": true' in rendered


def test_failure_report_keeps_category_and_drops_raw_exception():
    run = _run(
        status="failed",
        output=None,
        error="RuntimeError: ERROR_SECRET_MARKER_6c70 /private/repository/path",
    )
    report = build_report(
        _dataset(), BaselineConfig("safe", "v1", "model", "eval-v1", 1), [run]
    )
    rendered = report.to_json() + report.to_markdown()

    assert "RuntimeError" in rendered
    assert "ERROR_SECRET_MARKER_6c70" not in rendered
    assert "/private/repository/path" not in rendered


def test_run_detail_uses_an_explicit_allowlist():
    report = build_report(
        _dataset(), BaselineConfig("safe", "v1", "model", "eval-v1", 1), [_run()]
    )
    detail = report.run_details[0]

    assert set(detail) == {
        "eval_run_id",
        "case_id",
        "trial_no",
        "status",
        "input_hash",
        "output_hash",
        "duration_ms",
        "model_id",
        "prompt_version",
        "measurement_mode",
        "deterministic_only",
        "workflow_version",
        "route",
        "counters",
        "error_category",
    }
    assert "output" not in detail
    assert "token_usage" not in detail


def test_report_json_is_stable_across_metadata_insertion_order():
    first = _run()
    second = _run()
    reversed_metadata = dict(reversed(list(second.metadata.items())))
    second.metadata.clear()
    second.metadata.update(reversed_metadata)
    config = BaselineConfig("safe", "v1", "model", "eval-v1", 1)

    assert build_report(_dataset(), config, [first]).to_json() == build_report(
        _dataset(), config, [second]
    ).to_json()


def test_serialized_report_has_no_sensitive_keys_except_prompt_version():
    payload = json.loads(
        build_report(
            _dataset(), BaselineConfig("safe", "v1", "model", "eval-v1", 1), [_run()]
        ).to_json()
    )
    keys: set[str] = set()

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                keys.add(key.lower())
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    assert not keys.intersection(
        {"authorization", "api_key", "access_token", "refresh_token", "private_key", "prompt", "excerpt"}
    )
