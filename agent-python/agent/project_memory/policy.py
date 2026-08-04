from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectMemoryPolicy:
    version: str = "project-memory-policy.off"
    mode: str = "off"
    max_records: int = 12
    max_query_chars: int = 800

    def __post_init__(self) -> None:
        if self.mode not in {"off", "shadow", "enforce"}:
            raise ValueError("project memory mode must be off, shadow, or enforce")
        if not self.version or len(self.version) > 80:
            raise ValueError("project memory policy version must be bounded")
        if not 1 <= self.max_records <= 50:
            raise ValueError("project memory record limit must be between 1 and 50")
        if not 64 <= self.max_query_chars <= 4_000:
            raise ValueError("project memory query limit must be between 64 and 4000")

    @classmethod
    def from_environment(cls) -> "ProjectMemoryPolicy":
        mode = os.getenv("PRD_AGENT_PROJECT_MEMORY_MODE", "off").strip().lower()
        version = os.getenv(
            "PRD_AGENT_PROJECT_MEMORY_POLICY_VERSION",
            "project-memory-policy.off" if mode == "off" else "project-memory-policy.v1",
        ).strip()
        return cls(
            version=version,
            mode=mode,
            max_records=_env_int("PRD_AGENT_PROJECT_MEMORY_MAX_RECORDS", 12),
            max_query_chars=_env_int("PRD_AGENT_PROJECT_MEMORY_MAX_QUERY_CHARS", 800),
        )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
