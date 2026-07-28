from __future__ import annotations

import pytest
from pydantic import ValidationError

from prd_agent.production.dispatch import RunCommand
from prd_agent.production.queueing import (
    CeleryMessageBroker,
    RunCommandPayload,
    build_celery_app,
    register_execute_run_task,
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.commands = []

    def execute(self, command) -> str:
        self.commands.append(command)
        return "SUCCEEDED"


def test_celery_profile_accepts_only_json_and_routes_isolated_workloads():
    app = build_celery_app(
        broker_url="memory://",
        result_backend="cache+memory://",
    )

    assert app.conf.accept_content == ["json"]
    assert app.conf.task_serializer == "json"
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_routes["prd_agent.execute_run"]["queue"] == "agent.run"
    assert (
        app.conf.task_routes["prd_agent.integration.write"]["queue"]
        == "integration.write"
    )
    assert app.conf.task_routes["prd_agent.eval"]["queue"] == "eval"


def test_execute_run_task_rejects_payload_with_sensitive_extra_fields():
    with pytest.raises(ValidationError):
        RunCommandPayload.model_validate(
            {
                "message_id": "message-1",
                "payload_version": 1,
                "run_id": "run-1",
                "reason": "START_OR_RESUME",
                "token": "must-not-enter-broker",
            }
        )


def test_eager_celery_task_validates_and_calls_the_run_executor():
    app = build_celery_app(
        broker_url="memory://",
        result_backend="cache+memory://",
    )
    app.conf.task_always_eager = True
    executor = RecordingExecutor()
    task = register_execute_run_task(app, lambda: executor)
    payload = {
        "message_id": "message-1",
        "payload_version": 1,
        "run_id": "run-1",
        "reason": "START_OR_RESUME",
    }

    result = task.apply(args=[payload]).get()

    assert result == "SUCCEEDED"
    assert executor.commands == [RunCommand(**payload)]


def test_broker_adapter_sends_only_the_minimal_versioned_command():
    sent = []

    class FakeCelery:
        def send_task(self, name, *, kwargs, queue):
            sent.append((name, kwargs, queue))

    broker = CeleryMessageBroker(FakeCelery())
    broker.publish(
        RunCommand(
            message_id="message-1",
            payload_version=1,
            run_id="run-1",
            reason="RECOVER",
        )
    )

    assert sent == [
        (
            "prd_agent.execute_run",
            {
                "command": {
                    "message_id": "message-1",
                    "payload_version": 1,
                    "run_id": "run-1",
                    "reason": "RECOVER",
                }
            },
            "agent.run",
        )
    ]
