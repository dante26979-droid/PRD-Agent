from __future__ import annotations

import pytest

from prd_agent.production.audit import (
    AuditEvent,
    AuditUnavailableError,
    perform_audited_write,
)


class FailingAuditStore:
    def append(self, event):
        raise OSError("audit backend unavailable")


def test_high_risk_write_fails_closed_before_provider_side_effect():
    calls = []
    event = AuditEvent(
        audit_event_id="audit-1",
        tenant_id="tenant-a",
        principal_hash="sha256:" + "a" * 64,
        action="EXPORT_CREATE",
        target_type="TASK",
        target_id="task-1",
        correlation_id="request-1",
    )

    with pytest.raises(AuditUnavailableError):
        perform_audited_write(
            audit_store=FailingAuditStore(),
            intent=event,
            action=lambda: calls.append("provider-called"),
        )

    assert calls == []
    assert "token" not in repr(event).lower()
    assert "content" not in repr(event).lower()
