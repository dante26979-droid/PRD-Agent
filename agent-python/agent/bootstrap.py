from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Callable

import httpx

from agent.v1 import agent_execution_pb2 as proto

from .checkpoint import CheckpointCodec
from .config import AgentSettings
from .context import RunContext
from .evidence import repository_hits_to_evidence
from .graph import LangGraphAgentLoop
from .investigation import InvestigationPlan
from .investigation.models import InvestigationBudget
from .model import DeepSeekChatClient, ModelResponse
from .quality import DraftQualityPolicy
from .result import AgentResult


@dataclass(frozen=True)
class DeterministicAgentLoop:
    """Local/test Agent Loop adapter with no external or durable dependencies."""

    checkpoint_codec: CheckpointCodec
    quality_policy: DraftQualityPolicy

    def __call__(
        self,
        context: RunContext,
        cancel_event: threading.Event | None = None,
    ) -> AgentResult:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("agent run was cancelled")
        resumed = _resume_state(self.checkpoint_codec, context)
        message = context.task_message.strip()
        if not message:
            raise ValueError("task message is required")
        request_hash = "sha256:" + hashlib.sha256(message.encode("utf-8")).hexdigest()
        next_sequence = max(context.checkpoint_sequence, 0) + 1
        task_version = max(
            resumed.task_version if resumed is not None else getattr(context, "task_version", 1),
            1,
        )
        checkpoint = self.checkpoint_codec.encode(
            workflow_version=context.workflow_version or "agent-runtime.v1",
            run_id=context.run_id,
            task_version=task_version,
            sequence=next_sequence,
            payload={"stage": "DRAFTED", "request_hash": request_hash},
        )
        markdown = self.quality_policy.validate(
            (
                "# PRD Working Draft\n\n"
                "## 需求背景\n\n"
                f"{message}\n\n"
                "## 待确认内容\n\n"
                "- 目标用户与核心场景\n"
                "- 功能范围与验收标准\n"
                "- Repository Evidence 与风险\n"
            )
        )
        draft = {
            "task_id": context.task_id,
            "run_id": context.run_id,
            "markdown": markdown,
        }
        return AgentResult(
            attempt=proto.RecordModelAttemptRequest(
                attempt_key=f"{context.run_id}:draft:{next_sequence}",
                operation="generate_working_draft",
                prompt_version="deterministic-agent.v1",
                provider="deterministic",
                request_hash=request_hash,
                status="SUCCEEDED",
                response_metadata_json=json.dumps(
                    {"workflow_version": context.workflow_version or "agent-runtime.v1"},
                    sort_keys=True,
                ),
                token_usage_json="{}",
            ),
            checkpoint_sequence=next_sequence,
            checkpoint=checkpoint,
            draft_key=f"{context.run_id}:draft:{next_sequence}",
            expected_task_version=task_version,
            draft_patch=json.dumps(
                draft,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )


@dataclass(frozen=True)
class DeterministicStructuredModel:
    """Offline JSON model used to exercise the production graph locally."""

    def complete(self, system_prompt: str, user_prompt: str) -> ModelResponse:
        request = json.loads(user_prompt)
        task_message = str(request.get("task_message", "")).strip()
        if request.get("quality_issues"):
            markdown = str(request.get("markdown", "")).strip()
        else:
            markdown = (
                "# PRD Working Draft\n\n"
                "## 需求背景\n\n"
                f"{task_message}\n\n"
                "## 功能范围\n\n"
                "- 生成结构化 PRD 草稿。\n"
                "- 对草稿执行事实校验和质量检查。\n\n"
                "## 验收标准\n\n"
                "- 每个确认单元可以独立评审。\n"
                "- 运行快照可用于重启恢复。\n"
            )
        return ModelResponse(
            output=json.dumps({"markdown": markdown}, ensure_ascii=False),
            token_usage={"total_tokens": 0},
            model_id="deterministic-structured-v1",
        )


@dataclass(frozen=True)
class RemoteAgentLoop:
    model: DeepSeekChatClient
    checkpoint_codec: CheckpointCodec
    quality_policy: DraftQualityPolicy
    capability_factory: Callable[[RunContext], object] | None = None
    run_token_budget: int = 24000

    def __call__(
        self,
        context: RunContext,
        cancel_event: threading.Event | None = None,
    ) -> AgentResult:
        _raise_if_cancelled(cancel_event)
        resumed = _resume_state(self.checkpoint_codec, context)
        request_payload = {
            "run_id": context.run_id,
            "task_id": context.task_id,
            "task_message": context.task_message,
            "workflow_version": context.workflow_version or "agent-runtime.v1",
        }
        if context.repository_binding_id and context.repository_revision:
            request_payload["repository"] = {
                "binding_id": context.repository_binding_id,
                "revision": context.repository_revision,
            }
        request_json = json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_hash = "sha256:" + hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        first_operation = "plan_or_generate_working_draft"
        _plan_model_attempt(
            context,
            operation=first_operation,
            attempt_sequence=1,
            request_hash=request_hash,
        )
        response = self.model.complete(
            (
                "You are the PRD Agent runtime. Return JSON. You may return a "
                "non-empty markdown field directly, or repository_queries "
                "and prd_query to request grounded evidence. Do not include "
                "credentials or hidden reasoning."
            ),
            request_json,
        )
        _raise_if_cancelled(cancel_event)
        structured = _structured_output(response)
        attempts = []
        evidence = ()
        total_tokens = _total_tokens(response)
        if not isinstance(structured.get("markdown"), str):
            plan = InvestigationPlan.from_model_output(structured)
            if not plan.repository_queries and not plan.prd_query:
                raise ValueError(
                    "model response requires markdown or an investigation plan"
                )
            plan_attempt = _model_attempt(
                context,
                operation=first_operation,
                attempt_sequence=1,
                request_hash=request_hash,
                response=response,
            )
            evidence = self._collect_evidence(
                context,
                plan,
                cancel_event,
            )
            grounded_payload = {
                **request_payload,
                "evidence": [
                    {
                        "source_type": item.source_type,
                        "source_id": item.source_id,
                        "locator": item.locator,
                        "excerpt": item.excerpt,
                        "excerpt_hash": item.excerpt_hash,
                    }
                    for item in evidence
                ],
            }
            grounded_json = json.dumps(
                grounded_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            grounded_hash = "sha256:" + hashlib.sha256(
                grounded_json.encode("utf-8")
            ).hexdigest()
            _plan_model_attempt(
                context,
                operation="generate_working_draft",
                attempt_sequence=2,
                request_hash=grounded_hash,
            )
            response = self.model.complete(
                (
                    "Generate a reviewable PRD Working Draft as JSON with a "
                    "non-empty markdown field. Use only the supplied evidence "
                    "for repository claims and preserve evidence locators."
                ),
                grounded_json,
            )
            _raise_if_cancelled(cancel_event)
            structured = _structured_output(response)
            attempts.append(
                _model_attempt(
                    context,
                    operation="generate_working_draft",
                    attempt_sequence=2,
                    request_hash=grounded_hash,
                    response=response,
                )
            )
            total_tokens += _total_tokens(response)
            first_attempt = plan_attempt
        else:
            first_attempt = _model_attempt(
                context,
                operation=first_operation,
                attempt_sequence=1,
                request_hash=request_hash,
                response=response,
            )
        if total_tokens > self.run_token_budget:
            raise ValueError("Agent Run token budget exceeded")
        if not isinstance(structured.get("markdown"), str):
            raise ValueError("model response requires a markdown field")
        markdown = self.quality_policy.validate(structured["markdown"])
        next_sequence = max(context.checkpoint_sequence, 0) + 1
        task_version = max(
            resumed.task_version if resumed is not None else getattr(context, "task_version", 1),
            1,
        )
        output_hash = "sha256:" + hashlib.sha256(response.output.encode("utf-8")).hexdigest()
        checkpoint = self.checkpoint_codec.encode(
            workflow_version=context.workflow_version or "agent-runtime.v1",
            run_id=context.run_id,
            task_version=task_version,
            sequence=next_sequence,
            payload={
                "stage": "DRAFTED",
                "request_hash": request_hash,
                "output_hash": output_hash,
            },
        )
        draft = {
            "task_id": context.task_id,
            "run_id": context.run_id,
            "markdown": markdown,
        }
        return AgentResult(
            attempt=first_attempt,
            additional_attempts=tuple(attempts),
            evidence=evidence,
            checkpoint_sequence=next_sequence,
            checkpoint=checkpoint,
            draft_key=f"{context.run_id}:draft:{next_sequence}",
            expected_task_version=task_version,
            draft_patch=json.dumps(
                draft,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _collect_evidence(
        self,
        context: RunContext,
        plan: InvestigationPlan,
        cancel_event,
    ) -> tuple[proto.EvidenceItem, ...]:
        if self.capability_factory is None:
            raise ValueError("investigation requires Capability Gateway")
        if context.lease is None or not context.dispatch_id:
            raise ValueError("Agent Run lacks capability lease context")
        if plan.repository_queries and (
            not context.repository_binding_id or not context.repository_revision
        ):
            raise ValueError("Agent Run lacks repository capability context")
        gateway = self.capability_factory(context)
        hits = []
        evidence = []
        try:
            for query in plan.repository_queries:
                _raise_if_cancelled(cancel_event)
                query_hits = gateway.search_repository(
                    binding_id=context.repository_binding_id,
                    revision=context.repository_revision,
                    query=query,
                    limit=10,
                )
                hits.extend(query_hits)
                if len(hits) >= 100:
                    break
            if hits:
                evidence.extend(
                    repository_hits_to_evidence(
                        context.repository_binding_id,
                        context.repository_revision,
                        hits,
                        limit=100,
                    )
                )
            if plan.prd_query and len(evidence) < 100:
                _raise_if_cancelled(cancel_event)
                catalog_hits = gateway.search_prd_catalog(
                    query=plan.prd_query,
                    limit=10,
                )
                locator_ids = [item.locator_id for item in catalog_hits if item.locator_id]
                if locator_ids:
                    sections = gateway.fetch_prd_sections(locator_ids)
                    for section in sections:
                        excerpt = section.markdown[:8000]
                        evidence.append(
                            proto.EvidenceItem(
                                source_type="prd",
                                source_id=section.source_revision,
                                locator=section.locator_id,
                                excerpt_hash="sha256:"
                                + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                                excerpt=excerpt,
                            )
                        )
                        if len(evidence) >= 100:
                            break
            return tuple(evidence)
        finally:
            close = getattr(gateway, "close", None)
            if callable(close):
                close()


def build_agent_loop(
    *,
    transport: httpx.BaseTransport | None = None,
    capability_factory: Callable[[RunContext], object] | None = None,
):
    """Build the default local/test loop.

    Production model and capability wiring is added behind this same factory,
    so the Worker bootstrap remains stable.
    """

    settings = AgentSettings.load()
    if settings.llm is not None:
        if capability_factory is None and settings.capability_target:
            capability_factory = _capability_factory(
                settings.capability_target,
                settings.service_token,
            )
        if settings.loop_mode == "legacy":
            return RemoteAgentLoop(
                DeepSeekChatClient(settings.llm, transport=transport),
                CheckpointCodec(),
                DraftQualityPolicy(max_bytes=settings.max_draft_bytes),
                capability_factory=capability_factory,
                run_token_budget=settings.llm.run_token_budget,
            )
        return LangGraphAgentLoop(
            model=DeepSeekChatClient(settings.llm, transport=transport),
            checkpoint_codec=CheckpointCodec(),
            quality_policy=DraftQualityPolicy(max_bytes=settings.max_draft_bytes),
            capability_factory=capability_factory,
            budget=InvestigationBudget(
                max_iterations=settings.llm.max_iterations,
                max_tool_calls=settings.llm.max_tool_calls,
                token_budget=settings.llm.run_token_budget,
                no_progress_limit=settings.llm.no_progress_limit,
                max_replans=settings.llm.max_replans,
            ),
            advanced_loop_mode=settings.advanced_loop_mode,
            max_supplements=settings.llm.max_supplements,
            max_quality_repairs=settings.llm.max_quality_repairs,
        )
    if settings.advanced_loop_mode == "enforce":
        return LangGraphAgentLoop(
            model=DeterministicStructuredModel(),
            checkpoint_codec=CheckpointCodec(),
            quality_policy=DraftQualityPolicy(max_bytes=settings.max_draft_bytes),
            required_coverage=(),
            advanced_loop_mode="enforce",
            max_supplements=0,
            max_quality_repairs=1,
        )
    return DeterministicAgentLoop(
        CheckpointCodec(),
        DraftQualityPolicy(max_bytes=settings.max_draft_bytes),
    )


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RuntimeError("agent run was cancelled")


def _resume_state(codec: CheckpointCodec, context: RunContext):
    if not context.checkpoint:
        if context.checkpoint_sequence:
            from .checkpoint import CheckpointError

            raise CheckpointError("checkpoint sequence exists without payload")
        return None
    state = codec.decode(context.checkpoint)
    if (
        state.run_id != context.run_id
        or state.workflow_version != (context.workflow_version or "agent-runtime.v1")
        or state.sequence != context.checkpoint_sequence
    ):
        from .checkpoint import CheckpointError

        raise CheckpointError("checkpoint does not match Agent Run")
    return state


def _structured_output(response: ModelResponse) -> dict:
    try:
        structured = json.loads(response.output)
    except json.JSONDecodeError as error:
        raise ValueError("model returned invalid JSON") from error
    if not isinstance(structured, dict):
        raise ValueError("model returned a non-object JSON value")
    return structured


def _model_attempt(
    context: RunContext,
    *,
    operation: str,
    attempt_sequence: int,
    request_hash: str,
    response: ModelResponse,
) -> proto.RecordModelAttemptRequest:
    output_hash = "sha256:" + hashlib.sha256(response.output.encode("utf-8")).hexdigest()
    return proto.RecordModelAttemptRequest(
        attempt_key=f"{context.run_id}:{operation}:{attempt_sequence}",
        operation=operation,
        prompt_version=f"agent-runtime.{operation}.v1",
        provider="deepseek",
        request_hash=request_hash,
        status="SUCCEEDED",
        response_metadata_json=json.dumps(
            {
                "model_id": response.model_id,
                "finish_reason": response.finish_reason,
                "provider_request_id": response.provider_request_id,
                "latency_ms": response.latency_ms,
                "output_hash": output_hash,
            },
            sort_keys=True,
        ),
        token_usage_json=json.dumps(response.token_usage, sort_keys=True),
    )


def _plan_model_attempt(
    context: RunContext,
    *,
    operation: str,
    attempt_sequence: int,
    request_hash: str,
) -> None:
    if context.plan_model_attempt is None:
        return
    context.plan_model_attempt(
        proto.RecordModelAttemptRequest(
            attempt_key=f"{context.run_id}:{operation}:{attempt_sequence}",
            operation=operation,
            prompt_version=f"agent-runtime.{operation}.v1",
            provider="deepseek",
            request_hash=request_hash,
            status="PLANNED",
            response_metadata_json="{}",
            token_usage_json="{}",
        )
    )


def _total_tokens(response: ModelResponse) -> int:
    usage = response.token_usage
    value = usage.get("total_tokens") or usage.get("total")
    if isinstance(value, int):
        return value
    return sum(item for item in usage.values() if isinstance(item, int))


def _capability_factory(target: str, service_token: str | None):
    from .capability import CapabilityGatewayClient, CapabilitySession

    def connect(context: RunContext):
        if context.lease is None:
            raise ValueError("Capability Gateway requires an Agent Run lease")
        return CapabilityGatewayClient.connect(
            target,
            CapabilitySession(
                lease=context.lease,
                request_id_prefix=context.dispatch_id or context.run_id,
                correlation_id=context.run_id,
            ),
            service_token=service_token,
        )

    return connect
