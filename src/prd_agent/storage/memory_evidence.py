from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone


class InMemoryEvidenceStore:
    def __init__(self) -> None:
        self._calls = {}
        self._inputs = {}
        self._replays = {}
        self._snapshots = {}

    def save_snapshot(self, snapshot) -> None:
        key = (
            snapshot.repository_id,
            snapshot.resolved_commit_sha,
            snapshot.allowed_prefix,
        )
        self._snapshots[key] = deepcopy(snapshot)

    def get_replay(self, actor_id: str, idempotency_key: str):
        value = self._replays.get((actor_id, idempotency_key))
        return deepcopy(value) if value else None

    def save_running(self, call, input_hash: str) -> None:
        self._calls[call.tool_call_id] = deepcopy(call)
        self._inputs[call.tool_call_id] = input_hash

    def complete(
        self, actor_id: str, idempotency_key: str, input_hash: str, execution
    ) -> None:
        self._calls[execution.tool_call.tool_call_id] = deepcopy(execution.tool_call)
        self._replays[(actor_id, idempotency_key)] = (
            input_hash,
            deepcopy(execution),
        )

    def get_execution(self, tool_call_id: str):
        for _, execution in self._replays.values():
            if execution.tool_call.tool_call_id == tool_call_id:
                return deepcopy(execution)
        raise KeyError(tool_call_id)

    def all_calls(self):
        return tuple(deepcopy(list(self._calls.values())))

    def executions_for_investigation(self, investigation_id: str):
        return tuple(
            self.get_execution(call.tool_call_id)
            for call in self.all_calls()
            if call.investigation_id == investigation_id
            and call.status.value
            in {"SUCCEEDED", "PARTIAL", "EMPTY", "FAILED", "BLOCKED"}
        )

    def recover_stale_running(self, *, stale_before):
        recovered = []
        now = datetime.now(timezone.utc)
        for tool_call_id, call in tuple(self._calls.items()):
            if call.status.value == "RUNNING" and call.started_at < stale_before:
                self._calls[tool_call_id] = call.model_copy(
                    update={
                        "status": "FAILED",
                        "public_summary": "工具调用执行进程失联",
                        "error_code": "WORKER_LOST",
                        "ended_at": now,
                    }
                )
                recovered.append(tool_call_id)
        return tuple(recovered)
