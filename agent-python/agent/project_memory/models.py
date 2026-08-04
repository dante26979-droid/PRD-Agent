from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class MemoryBundle:
    schema_version: str
    bundle_id: str
    run_id: str
    operation: str
    operation_sequence: int
    memory_space_id: str
    memory_watermark: int
    memory_policy_version: str
    memory_assignment_hash: str
    query: str
    records: tuple[Mapping[str, object], ...]
    conflicts: tuple[Mapping[str, object], ...]
    excluded_count: int
    source_set_hash: str
    bundle_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "run_id": self.run_id,
            "operation": self.operation,
            "operation_sequence": self.operation_sequence,
            "memory_space_id": self.memory_space_id,
            "memory_watermark": self.memory_watermark,
            "memory_policy_version": self.memory_policy_version,
            "memory_assignment_hash": self.memory_assignment_hash,
            "query": self.query,
            "records": [dict(item) for item in self.records],
            "conflicts": [dict(item) for item in self.conflicts],
            "excluded_count": self.excluded_count,
            "source_set_hash": self.source_set_hash,
            "bundle_hash": self.bundle_hash,
        }


@dataclass(frozen=True)
class PreparedProjectMemory:
    payload: Mapping[str, object]
    bundle: MemoryBundle | None = None
    capability_artifact_key: str = ""
    capability_artifact_hash: str = ""
    bundle_artifact_key: str = ""
    bundle_artifact_hash: str = ""
    replayed: bool = False
