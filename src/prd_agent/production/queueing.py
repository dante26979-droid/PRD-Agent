"""Celery/Redis transport adapters for production execution commands."""

from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.production.dispatch import RunCommand


class RunCommandPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(min_length=1, max_length=200)
    payload_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    reason: Literal["START_OR_RESUME", "RECOVER", "RETRY"]

    def to_command(self) -> RunCommand:
        return RunCommand(**self.model_dump())


def build_celery_app(*, broker_url: str, result_backend: str | None = None):
    try:
        from celery import Celery
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Celery support requires: python -m pip install '.[production]'"
        ) from exc
    app = Celery(
        "prd_agent",
        broker=broker_url,
        backend=result_backend,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        worker_prefetch_multiplier=1,
        task_track_started=True,
        broker_connection_retry_on_startup=True,
        task_routes={
            "prd_agent.execute_run": {"queue": "agent.run"},
            "prd_agent.integration.read": {"queue": "integration.read"},
            "prd_agent.integration.write": {"queue": "integration.write"},
            "prd_agent.eval": {"queue": "eval"},
        },
    )
    return app


def register_execute_run_task(app, executor_factory: Callable[[], object]):
    @app.task(
        name="prd_agent.execute_run",
        acks_late=True,
        reject_on_worker_lost=True,
        soft_time_limit=300,
        time_limit=330,
        typing=False,
    )
    def execute_run(command: dict) -> str:
        payload = RunCommandPayload.model_validate(command)
        executor = executor_factory()
        return executor.execute(payload.to_command())

    return execute_run


class CeleryMessageBroker:
    def __init__(self, celery_app) -> None:
        self.celery_app = celery_app

    def publish(self, command: RunCommand) -> None:
        payload = RunCommandPayload(
            message_id=command.message_id,
            payload_version=command.payload_version,
            run_id=command.run_id,
            reason=command.reason,
        )
        self.celery_app.send_task(
            "prd_agent.execute_run",
            kwargs={"command": payload.model_dump(mode="json")},
            queue="agent.run",
        )
