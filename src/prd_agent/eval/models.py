"""Data contracts for the M0 evaluation dataset and baseline runs.

The module intentionally uses the standard library so the offline Stub Model and
dataset checks can run before optional PostgreSQL/model dependencies are installed.
The public contracts are immutable dataclasses and validate at construction time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping

from prd_agent.model_api.models import ModelResponse


NEED_TYPES = frozenset({"OPTIONAL", "REQUIRED", "NOT_REQUIRED"})


from prd_agent.hashing import canonical_json, sha256_json


@dataclass(frozen=True)
class InformationNeed:
    kind: str
    category: str
    reason: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InformationNeed":
        kind = str(value.get("type", ""))
        category = str(value.get("category", ""))
        reason = str(value.get("reason", ""))
        if kind not in NEED_TYPES or not category or not reason:
            raise ValueError("information need requires valid type, category and reason")
        return cls(kind=kind, category=category, reason=reason)


@dataclass(frozen=True)
class ExpectedFact:
    fact_id: str
    statement: str
    source_locators: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExpectedFact":
        fact_id = str(value.get("fact_id", ""))
        statement = str(value.get("statement", ""))
        locators = tuple(str(item) for item in value.get("source_locators", []))
        if not fact_id or not statement:
            raise ValueError("expected fact requires fact_id and statement")
        return cls(fact_id=fact_id, statement=statement, source_locators=locators)


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    title: str
    requirement: str
    context: str
    tags: tuple[str, ...]
    repository_id: str
    resolved_commit_sha: str
    expected_information_needs: tuple[InformationNeed, ...]
    required_sources: tuple[Mapping[str, Any], ...]
    expected_facts: tuple[ExpectedFact, ...]
    expected_unknowns: tuple[str, ...]
    expected_conflicts: tuple[Any, ...]
    required_prd_sections: tuple[str, ...]
    clarification_questions: tuple[str, ...]
    expected_keywords: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvalCase":
        required = (
            "case_id",
            "title",
            "requirement",
            "context",
            "repository_id",
            "resolved_commit_sha",
        )
        missing = [key for key in required if not str(value.get(key, "")).strip()]
        if missing:
            raise ValueError("case missing required fields: " + ", ".join(missing))
        commit = str(value["resolved_commit_sha"])
        if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower()):
            raise ValueError("resolved_commit_sha must be a 40-character hexadecimal SHA")

        needs = tuple(
            InformationNeed.from_dict(item)
            for item in value.get("expected_information_needs", [])
        )
        facts = tuple(ExpectedFact.from_dict(item) for item in value.get("expected_facts", []))
        sections = tuple(str(item) for item in value.get("required_prd_sections", []))
        if not sections:
            raise ValueError("case requires at least one required_prd_section")
        return cls(
            case_id=str(value["case_id"]),
            title=str(value["title"]),
            requirement=str(value["requirement"]),
            context=str(value["context"]),
            tags=tuple(str(item) for item in value.get("tags", [])),
            repository_id=str(value["repository_id"]),
            resolved_commit_sha=commit,
            expected_information_needs=needs,
            required_sources=tuple(value.get("required_sources", [])),
            expected_facts=facts,
            expected_unknowns=tuple(str(item) for item in value.get("expected_unknowns", [])),
            expected_conflicts=tuple(value.get("expected_conflicts", [])),
            required_prd_sections=sections,
            clarification_questions=tuple(
                str(item) for item in value.get("clarification_questions", [])
            ),
            expected_keywords=tuple(str(item) for item in value.get("expected_keywords", [])),
        )

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "requirement": self.requirement,
            "context": self.context,
            "tags": list(self.tags),
            "repository_id": self.repository_id,
            "resolved_commit_sha": self.resolved_commit_sha,
        }


@dataclass(frozen=True)
class EvalDataset:
    dataset_version: str
    repository_id: str
    resolved_commit_sha: str
    cases: tuple[EvalCase, ...]


@dataclass(frozen=True)
class BaselineConfig:
    config_id: str
    prompt_version: str
    model_id: str
    dataset_version: str
    trials_per_case: int = 3
    timeout_seconds: float = 120.0
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.config_id or not self.prompt_version or not self.model_id:
            raise ValueError("baseline config requires identifiers")
        if self.trials_per_case < 1:
            raise ValueError("trials_per_case must be positive")


@dataclass(frozen=True)
class BaselineRun:
    eval_run_id: str
    case_id: str
    config_id: str
    trial_no: int
    model_id: str
    prompt_version: str
    dataset_version: str
    repository_commit: str
    input_hash: str
    output_hash: str | None
    started_at: datetime
    duration_ms: int
    token_usage: Mapping[str, Any]
    status: str
    output: str | None = None
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["started_at"] = self.started_at.isoformat()
        return value
