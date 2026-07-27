from __future__ import annotations

from collections import defaultdict
import uuid

from prd_agent.hashing import canonical_json

from .models import DeterministicFact, SourceConflict, VerificationStatus


class DeterministicConflictDetector:
    def detect(
        self, facts: tuple[DeterministicFact, ...]
    ) -> tuple[tuple[DeterministicFact, ...], tuple[SourceConflict, ...]]:
        grouped: dict[tuple[str, str], list[DeterministicFact]] = defaultdict(list)
        for fact in facts:
            grouped[(fact.subject, fact.predicate)].append(fact)
        conflicting_ids: set[str] = set()
        conflicts = []
        for (subject, predicate), group in grouped.items():
            values = {canonical_json(item.value_json) for item in group}
            if len(values) < 2:
                continue
            ids = tuple(item.fact_id for item in group)
            conflicting_ids.update(ids)
            conflicts.append(
                SourceConflict(
                    conflict_id=f"conflict-{uuid.uuid4().hex}",
                    subject=subject,
                    description=f"{subject} 的 {predicate} 存在不同确定性值",
                    fact_ids=ids,
                )
            )
        updated = tuple(
            fact.model_copy(
                update={"verification_status": VerificationStatus.CONFLICTING}
            )
            if fact.fact_id in conflicting_ids
            else fact
            for fact in facts
        )
        return updated, tuple(conflicts)
