from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CheckpointCompatibilityError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class GraphVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "GraphVersion":
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
        if match is None:
            raise ValueError("graph version must use major.minor.patch")
        return cls(*(int(item) for item in match.groups()))


class CheckpointEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    graph_name: str = Field(min_length=1, max_length=100)
    graph_version: str
    task_id: str = Field(min_length=1, max_length=200)
    task_version: int = Field(ge=1)
    cursor: str = Field(min_length=1, max_length=200)


def build_thread_id(task_id: str, graph_name: str, major_version: int) -> str:
    if not task_id or not graph_name or major_version < 1:
        raise ValueError("task, graph, and positive major version are required")
    canonical = f"{task_id}\0{graph_name}\0{major_version}".encode("utf-8")
    return "thread-" + hashlib.sha256(canonical).hexdigest()[:40]


def validate_checkpoint(
    payload: dict,
    *,
    current: GraphVersion,
) -> CheckpointEnvelope:
    try:
        envelope = CheckpointEnvelope.model_validate(payload)
        saved = GraphVersion.parse(envelope.graph_version)
    except (ValidationError, ValueError) as exc:
        raise CheckpointCompatibilityError("invalid checkpoint envelope") from exc
    if saved.major != current.major or saved.minor > current.minor:
        raise CheckpointCompatibilityError("checkpoint graph version is incompatible")
    return envelope
