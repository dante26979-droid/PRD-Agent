from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from threading import RLock

from prd_agent.integrations.errors import IntegrationError, IntegrationErrorCode

from .models import ExportIntent, ExportRun, ExternalDocumentBinding


class InMemoryExportStore:
    def __init__(self) -> None:
        self._intents: dict[str, ExportIntent] = {}
        self._bindings: dict[tuple[str, str], ExternalDocumentBinding] = {}
        self._runs: dict[str, ExportRun] = {}
        self._replays: dict[tuple[str, str], tuple[str, str]] = {}
        self._preview_replays: dict[tuple[str, str], tuple[str, str]] = {}
        self._intent_claims: dict[str, tuple[str, str, datetime]] = {}
        self._lock = RLock()

    def save_intent(self, intent: ExportIntent) -> None:
        with self._lock:
            self._intents[intent.intent_id] = deepcopy(intent)

    def get_intent(self, intent_id: str, owner_id: str) -> ExportIntent:
        value = self._intents.get(intent_id)
        if value is None or value.owner_id != owner_id:
            raise IntegrationError(
                IntegrationErrorCode.RESOURCE_NOT_FOUND,
                "export intent was not found",
            )
        return deepcopy(value)

    def get_preview_replay(self, owner_id: str, idempotency_key: str):
        value = self._preview_replays.get((owner_id, idempotency_key))
        if value is None:
            return None
        input_hash, intent_id = value
        return input_hash, self.get_intent(intent_id, owner_id)

    def save_preview_replay(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        intent: ExportIntent,
    ) -> None:
        with self._lock:
            key = (owner_id, idempotency_key)
            current = self._preview_replays.get(key)
            if current and current[0] != input_hash:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key was used with different preview input",
                )
            self._preview_replays[key] = (input_hash, intent.intent_id)

    def begin_preview(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        intent: ExportIntent,
    ) -> ExportIntent | None:
        with self._lock:
            key = (owner_id, idempotency_key)
            current = self._preview_replays.get(key)
            if current:
                if current[0] != input_hash:
                    raise IntegrationError(
                        IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was used with different preview input",
                    )
                return deepcopy(self._intents[current[1]])
            self._intents[intent.intent_id] = deepcopy(intent)
            self._preview_replays[key] = (input_hash, intent.intent_id)
            return None

    def save_binding(self, binding: ExternalDocumentBinding) -> None:
        with self._lock:
            key = (binding.owner_id, binding.task_id)
            current = self._bindings.get(key)
            if current and current.binding_id != binding.binding_id:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "task already has a different document binding",
                )
            self._bindings[key] = deepcopy(binding)

    def get_binding(
        self,
        task_id: str,
        owner_id: str,
    ) -> ExternalDocumentBinding | None:
        value = self._bindings.get((owner_id, task_id))
        return deepcopy(value) if value else None

    def save_run(self, run: ExportRun) -> None:
        with self._lock:
            self._runs[run.export_run_id] = deepcopy(run)

    def begin_run(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        run: ExportRun,
    ) -> ExportRun | None:
        with self._lock:
            key = (owner_id, idempotency_key)
            current = self._replays.get(key)
            if current:
                if current[0] != input_hash:
                    raise IntegrationError(
                        IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was used with different input",
                    )
                return deepcopy(self._runs[current[1]])
            self._runs[run.export_run_id] = deepcopy(run)
            self._replays[key] = (input_hash, run.export_run_id)
            return None

    def get_replay(
        self,
        owner_id: str,
        idempotency_key: str,
    ) -> tuple[str, ExportRun] | None:
        value = self._replays.get((owner_id, idempotency_key))
        if value is None:
            return None
        input_hash, run_id = value
        return input_hash, deepcopy(self._runs[run_id])

    def save_replay(
        self,
        owner_id: str,
        idempotency_key: str,
        input_hash: str,
        run: ExportRun,
    ) -> None:
        with self._lock:
            key = (owner_id, idempotency_key)
            current = self._replays.get(key)
            if current and current[0] != input_hash:
                raise IntegrationError(
                    IntegrationErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency key was used with different input",
                )
            self._runs[run.export_run_id] = deepcopy(run)
            self._replays[key] = (input_hash, run.export_run_id)

    def consume_intent(self, intent: ExportIntent) -> None:
        with self._lock:
            self._intents[intent.intent_id] = deepcopy(intent)

    def claim_intent(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
        *,
        now: datetime,
        claim_expires_at: datetime,
    ) -> bool:
        with self._lock:
            intent = self._intents.get(intent_id)
            if (
                intent is None
                or intent.owner_id != owner_id
                or intent.consumed_at is not None
            ):
                return False
            current = self._intent_claims.get(intent_id)
            if (
                current is not None
                and current[1] != claim_id
                and current[2] > now
            ):
                return False
            self._intent_claims[intent_id] = (
                owner_id,
                claim_id,
                claim_expires_at,
            )
            return True

    def release_intent_claim(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
    ) -> None:
        with self._lock:
            current = self._intent_claims.get(intent_id)
            if current is not None and current[:2] == (owner_id, claim_id):
                del self._intent_claims[intent_id]

    def consume_claimed_intent(
        self,
        intent_id: str,
        owner_id: str,
        claim_id: str,
        *,
        consumed_at: datetime,
    ) -> bool:
        with self._lock:
            current = self._intent_claims.get(intent_id)
            intent = self._intents.get(intent_id)
            if (
                current is None
                or current[:2] != (owner_id, claim_id)
                or intent is None
                or intent.consumed_at is not None
            ):
                return False
            self._intents[intent_id] = intent.model_copy(
                update={"consumed_at": consumed_at}
            )
            del self._intent_claims[intent_id]
            return True

    def list_runs(self, task_id: str, owner_id: str) -> tuple[ExportRun, ...]:
        values = [
            run
            for run in self._runs.values()
            if run.task_id == task_id and run.owner_id == owner_id
        ]
        return tuple(deepcopy(sorted(values, key=lambda item: item.created_at, reverse=True)))
