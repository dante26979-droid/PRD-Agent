from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prd_agent.api.mapping import (
    decode_cursor,
    encode_cursor,
    public_event_payload,
)
from prd_agent.domain.entities import Task
from prd_agent.domain.errors import InvalidCommand


def test_task_cursor_round_trip():
    task = Task(
        "task-1",
        updated_at=datetime(2026, 7, 26, 12, 30, tzinfo=timezone.utc),
    )

    assert decode_cursor(encode_cursor(task)) == (task.updated_at, task.task_id)


@pytest.mark.parametrize("value", ["***", "W10", "WyIyMDI2LTA3LTI2IiwgIiJd"])
def test_invalid_task_cursor_is_rejected(value):
    with pytest.raises(InvalidCommand):
        decode_cursor(value)


def test_public_event_payload_uses_an_explicit_field_allowlist():
    assert public_event_payload(
        "ExportSucceeded",
        {
            "export_run_id": "run-1",
            "mode": "CREATE",
            "document_version": 3,
            "binding_id": "binding-1",
            "external_id": "private-document-token",
            "authorization": "Bearer secret",
        },
    ) == {
        "export_run_id": "run-1",
        "mode": "CREATE",
        "document_version": 3,
        "binding_id": "binding-1",
    }
