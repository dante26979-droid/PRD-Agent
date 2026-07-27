import unittest
from datetime import datetime, timedelta, timezone

from prd_agent.application.repository_evidence_service import ToolCallRecord, ToolCallStatus
from prd_agent.storage.memory_evidence import InMemoryEvidenceStore


class ToolCallRecoveryTests(unittest.TestCase):
    def test_stale_running_call_converges_to_failed_worker_lost(self) -> None:
        store = InMemoryEvidenceStore()
        call = ToolCallRecord(
            tool_call_id="call-1",
            actor_id="local",
            investigation_id="investigation-1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            tool_id="search_text",
            tool_schema_version="1",
            purpose="查找规则",
            arguments={"query": "amount"},
            action_signature="sha256:x",
            idempotency_key="key-1",
            status=ToolCallStatus.RUNNING,
            started_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        store.save_running(call, "sha256:input")

        recovered = store.recover_stale_running(
            stale_before=datetime.now(timezone.utc) - timedelta(minutes=5)
        )

        self.assertEqual(recovered, ("call-1",))
        saved = store.all_calls()[0]
        self.assertEqual(saved.status, ToolCallStatus.FAILED)
        self.assertEqual(saved.error_code, "WORKER_LOST")


if __name__ == "__main__":
    unittest.main()
