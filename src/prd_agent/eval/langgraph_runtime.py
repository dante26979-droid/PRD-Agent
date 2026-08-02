"""Evaluation baseline for the production LangGraph Agent loop."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

from .agent_trace import (
    AgentLoopTrace,
    FailureTrace,
    TRACE_SCHEMA_VERSION,
    TraceCounters,
)
from .models import AgentEvalMetadata, BaselineConfig, BaselineRun, EvalCase
from prd_agent.hashing import sha256_json


class AgentRuntimeUnavailable(RuntimeError):
    """The optional production Agent package is not importable."""


def _agent_dependencies():
    try:
        from agent.checkpoint import CheckpointCodec
        from agent.context import RunContext
        from agent.eval_adapter import TraceRuntimeEventSink, build_trace_payload
        from agent.graph import LangGraphAgentLoop
        from agent.investigation import InvestigationBudget
        from agent.quality import DraftQualityPolicy
        from agent.testing import (
            InstrumentedCapabilityGateway,
            InstrumentedModel,
            ScriptedAgentModel,
        )
    except ImportError as error:
        raise AgentRuntimeUnavailable(
            "LangGraph runtime baseline requires agent-python on PYTHONPATH"
        ) from error
    return {
        "CheckpointCodec": CheckpointCodec,
        "RunContext": RunContext,
        "TraceRuntimeEventSink": TraceRuntimeEventSink,
        "build_trace_payload": build_trace_payload,
        "LangGraphAgentLoop": LangGraphAgentLoop,
        "InvestigationBudget": InvestigationBudget,
        "DraftQualityPolicy": DraftQualityPolicy,
        "InstrumentedCapabilityGateway": InstrumentedCapabilityGateway,
        "InstrumentedModel": InstrumentedModel,
        "ScriptedAgentModel": ScriptedAgentModel,
    }


def _failure_category(error: Exception) -> str:
    code = getattr(error, "code", None)
    if code:
        return str(code).upper()
    if isinstance(error, json.JSONDecodeError):
        return "MODEL_INVALID_OUTPUT"
    if isinstance(error, ValueError) and "model" in str(error).lower():
        return "MODEL_INVALID_OUTPUT"
    return type(error).__name__.upper()


class LangGraphRuntimeBaseline:
    """Run fixed cases through the production graph using deterministic adapters."""

    def __init__(self, workspace_root: Path, fixture_path: Path | None = None) -> None:
        self.workspace_root = workspace_root.resolve()
        self.fixture_path = (
            fixture_path
            or self.workspace_root / "eval/fixtures/langgraph_v1_actions.json"
        ).resolve()
        try:
            value = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot load LangGraph fixture: {self.fixture_path}") from error
        if not isinstance(value, dict) or not str(value.get("fixture_version", "")):
            raise ValueError("LangGraph fixture requires fixture_version")
        self.fixture: dict[str, Any] = value

    @property
    def fixture_version(self) -> str:
        return str(self.fixture["fixture_version"])

    def _workflow_version(self, config: BaselineConfig) -> str:
        return str(config.options.get("workflow_version", "agent-runtime.v1"))

    def input_hash(self, case: EvalCase, config: BaselineConfig) -> str:
        return sha256_json(
            {
                "case": case.prompt_payload(),
                "workflow_version": self._workflow_version(config),
                "prompt_version": config.prompt_version,
                "fixture_version": self.fixture_version,
                "trace_schema": TRACE_SCHEMA_VERSION,
                "budget": {
                    key: config.options.get(key)
                    for key in (
                        "max_iterations",
                        "max_tool_calls",
                        "token_budget",
                        "no_progress_limit",
                        "max_replans",
                    )
                },
            }
        )

    def run_id(self, case: EvalCase, config: BaselineConfig, trial_no: int) -> str:
        if trial_no < 1:
            raise ValueError("trial_no must be positive")
        material = f"{config.config_id}:{case.case_id}:{trial_no}:{self.input_hash(case, config)}"
        return "run-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]

    def _case_fixture(self, case: EvalCase) -> Mapping[str, Any]:
        cases = self.fixture.get("cases")
        if not isinstance(cases, Mapping) or not isinstance(cases.get(case.case_id), Mapping):
            raise ValueError(f"LangGraph fixture missing case: {case.case_id}")
        return cases[case.case_id]

    def _outputs(self, case: EvalCase, fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        actions = fixture.get("actions", [])
        if not isinstance(actions, list):
            raise ValueError(f"invalid actions fixture for {case.case_id}")
        for item in actions:
            if not isinstance(item, Mapping):
                raise ValueError(f"invalid action fixture for {case.case_id}")
            coverage = str(item.get("target_coverage", "repository_evidence"))
            outputs.append(
                {
                    "action": {
                        "tool_id": "search_repository",
                        "tool_schema_version": "1",
                        "arguments": {"query": str(item["query"])},
                        "purpose": "Collect fixed characterization evidence",
                        "target_coverage": [coverage],
                    },
                    "_tokens": int(item.get("tokens", 2)),
                }
            )
        template = str(self.fixture.get("draft_template", "# PRD\n\n{{requirement}}"))
        markdown = (
            template.replace("{{title}}", case.title)
            .replace("{{requirement}}", case.requirement)
            .replace("{{context}}", case.context)
        )
        outputs.append({"markdown": markdown, "_tokens": int(fixture.get("draft_tokens", 5))})
        return outputs

    def _gateway_fixture(self, fixture: Mapping[str, Any]) -> dict[str, Any]:
        hits: dict[str, list[dict[str, Any]]] = {}
        for item in fixture.get("actions", []):
            query = str(item["query"])
            hits[query] = [dict(hit) for hit in item.get("hits", [])]
        return {"repository_hits": hits}

    def run_case(self, case, config, _model, *, trial_no: int) -> BaselineRun:
        deps = _agent_dependencies()
        fixture = self._case_fixture(case)
        input_hash = self.input_hash(case, config)
        eval_run_id = self.run_id(case, config, trial_no)
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        workflow_version = self._workflow_version(config)
        sink = deps["TraceRuntimeEventSink"]()
        gateway = deps["InstrumentedCapabilityGateway"].from_fixture(
            self._gateway_fixture(fixture)
        )
        model = deps["InstrumentedModel"](
            deps["ScriptedAgentModel"](self._outputs(case, fixture))
        )
        budget = deps["InvestigationBudget"](
            max_iterations=int(config.options.get("max_iterations", 5)),
            max_tool_calls=int(config.options.get("max_tool_calls", 12)),
            token_budget=int(config.options.get("token_budget", 12000)),
            no_progress_limit=int(config.options.get("no_progress_limit", 2)),
            max_replans=int(config.options.get("max_replans", 1)),
        )
        required_coverage = tuple(
            str(item) for item in fixture.get("required_coverage", [])
        )
        context = deps["RunContext"](
            run_id=eval_run_id,
            tenant_id="eval-tenant",
            owner_id="eval-owner",
            task_id=case.case_id,
            task_message=case.requirement + "\n\n" + case.context,
            workflow_version=workflow_version,
            checkpoint=b"",
            task_version=1,
            repository_binding_id=case.repository_id,
            repository_revision=case.resolved_commit_sha,
            event_sink=sink,
        )
        result = None
        failure: Exception | None = None
        try:
            result = deps["LangGraphAgentLoop"](
                model=model,
                checkpoint_codec=deps["CheckpointCodec"](),
                quality_policy=deps["DraftQualityPolicy"](),
                capability_factory=lambda _context: gateway,
                budget=budget,
                required_coverage=required_coverage,
            )(context)
        except Exception as error:  # noqa: BLE001 - failure becomes classified Eval data
            failure = error
        duration_ms = int((time.perf_counter() - started) * 1000)
        category = _failure_category(failure) if failure else None
        try:
            trace_payload = deps["build_trace_payload"](
                sink=sink,
                capability_observations=gateway.observations,
                result=result,
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                workflow_version=workflow_version,
                execution_mode="scripted_characterization",
                deterministic_only=True,
                started_at=started_at,
                duration_ms=duration_ms,
                input_hash=input_hash,
                failure_category=category,
                failure_retryable=bool(getattr(failure, "retryable", False)),
            )
            trace = AgentLoopTrace.from_dict(trace_payload)
        except Exception as trace_error:
            failure = failure or trace_error
            category = "INVALID_TRACE_SEQUENCE"
            trace = AgentLoopTrace(
                schema_version=TRACE_SCHEMA_VERSION,
                eval_run_id=eval_run_id,
                case_id=case.case_id,
                workflow_version=workflow_version,
                execution_mode="scripted_characterization",
                deterministic_only=True,
                status="failed",
                started_at=started_at,
                duration_ms=duration_ms,
                input_hash=input_hash,
                output_hash=None,
                result_outcome=None,
                stop_reason=None,
                counters=TraceCounters(),
                failure=FailureTrace(
                    category=category,
                    retryable=False,
                    public_code=category,
                ),
            )
        draft = json.loads(result.draft_patch) if result is not None else {}
        output = draft.get("markdown") if isinstance(draft, dict) else None
        metadata = AgentEvalMetadata(
            measurement_mode="scripted_characterization",
            deterministic_only=True,
            workflow_version=workflow_version,
            route=(
                "LEGACY_DIRECT_GENERATION"
                if not required_coverage
                else "LEGACY_BOUNDED_INVESTIGATION"
            ),
            trace=trace.as_dict(),
        )
        return BaselineRun(
            eval_run_id=eval_run_id,
            case_id=case.case_id,
            config_id=config.config_id,
            trial_no=trial_no,
            model_id="scripted-agent-model",
            prompt_version=config.prompt_version,
            dataset_version=config.dataset_version,
            repository_commit=case.resolved_commit_sha,
            input_hash=input_hash,
            output_hash=trace.output_hash,
            started_at=started_at,
            duration_ms=duration_ms,
            token_usage={"total_tokens": trace.counters.total_tokens},
            status="failed" if failure else "completed",
            output=output,
            error=category,
            metadata=metadata.as_dict(),
        )
