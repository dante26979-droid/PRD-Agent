from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence

from agent.draft import ClaimCriticality, ClaimType


class RunPurpose(StrEnum):
    PLAN_OUTLINE = "PLAN_OUTLINE"
    GENERATE_UNIT = "GENERATE_UNIT"
    REVISE_UNIT = "REVISE_UNIT"
    FULL_REVIEW = "FULL_REVIEW"


class RunOutputKind(StrEnum):
    OUTLINE_CANDIDATE = "OUTLINE_CANDIDATE"
    UNIT_CANDIDATE = "UNIT_CANDIDATE"
    UNIT_PATCH = "UNIT_PATCH"
    FULL_REVIEW_REPORT = "FULL_REVIEW_REPORT"


_OUTPUT_BY_PURPOSE = {
    RunPurpose.PLAN_OUTLINE: RunOutputKind.OUTLINE_CANDIDATE,
    RunPurpose.GENERATE_UNIT: RunOutputKind.UNIT_CANDIDATE,
    RunPurpose.REVISE_UNIT: RunOutputKind.UNIT_PATCH,
    RunPurpose.FULL_REVIEW: RunOutputKind.FULL_REVIEW_REPORT,
}


@dataclass(frozen=True)
class OutlineNode:
    node_key: str
    title: str
    ordinal: int
    unit_key: str
    parent_key: str = ""
    questions: tuple[str, ...] = ()
    required_content: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "node_key": self.node_key,
            "parent_key": self.parent_key,
            "ordinal": self.ordinal,
            "title": self.title,
            "questions": list(self.questions),
            "required_content": list(self.required_content),
            "unit_key": self.unit_key,
        }


@dataclass(frozen=True)
class OutlineUnit:
    unit_key: str
    title: str
    ordinal: int
    node_keys: tuple[str, ...]
    depends_on: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_key": self.unit_key,
            "title": self.title,
            "ordinal": self.ordinal,
            "node_keys": list(self.node_keys),
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class OutlineCandidate:
    title: str
    requirement_size: str
    nodes: tuple[OutlineNode, ...]
    units: tuple[OutlineUnit, ...]
    schema_version: str = "outline-candidate.v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_outline(self)
        object.__setattr__(self, "content_hash", _hash_json(self.payload()))

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "title": self.title,
            "requirement_size": self.requirement_size,
            "nodes": [item.as_dict() for item in self.nodes],
            "units": [item.as_dict() for item in self.units],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "OutlineCandidate":
        if str(value.get("schema_version", "outline-candidate.v1")) != "outline-candidate.v1":
            raise ValueError("unsupported outline candidate schema")
        nodes = tuple(
            OutlineNode(
                node_key=str(item.get("node_key", "")).strip(),
                parent_key=str(item.get("parent_key", "")).strip(),
                ordinal=int(item.get("ordinal", 0)),
                title=str(item.get("title", "")).strip(),
                questions=_strings(item.get("questions")),
                required_content=_strings(item.get("required_content")),
                unit_key=str(item.get("unit_key", "")).strip(),
            )
            for item in _mappings(value.get("nodes"), "nodes")
        )
        units = tuple(
            OutlineUnit(
                unit_key=str(item.get("unit_key", "")).strip(),
                title=str(item.get("title", "")).strip(),
                ordinal=int(item.get("ordinal", 0)),
                node_keys=_strings(item.get("node_keys")),
                depends_on=_strings(item.get("depends_on")),
            )
            for item in _mappings(value.get("units"), "units")
        )
        candidate = cls(
            title=str(value.get("title", "")).strip(),
            requirement_size=str(value.get("requirement_size", "")).strip(),
            nodes=nodes,
            units=units,
        )
        supplied = str(value.get("content_hash", ""))
        if supplied and supplied not in {candidate.content_hash, "sha256:" + candidate.content_hash}:
            raise ValueError("outline candidate content hash mismatch")
        return candidate


@dataclass(frozen=True)
class ConfirmedUnitContext:
    unit_key: str
    unit_version: int
    content_hash: str
    summary: str = ""
    working_draft_ref: str = ""
    markdown: str = ""

    def __post_init__(self) -> None:
        if not self.unit_key or self.unit_version < 1 or not _is_hash(self.content_hash):
            raise ValueError("confirmed unit context requires identity, version and hash")
        if self.markdown and _sha256(self.markdown) != _bare_hash(self.content_hash):
            raise ValueError("confirmed unit markdown hash mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_key": self.unit_key,
            "unit_version": self.unit_version,
            "content_hash": _bare_hash(self.content_hash),
            "summary": self.summary,
            "working_draft_ref": self.working_draft_ref,
            "markdown": self.markdown,
        }


@dataclass(frozen=True)
class UnitScope:
    purpose: RunPurpose
    outline_id: str = ""
    outline_version: int = 0
    outline_hash: str = ""
    current_unit_key: str = ""
    current_unit_title: str = ""
    current_unit_ordinal: int = 0
    section_node_keys: tuple[str, ...] = ()
    dependency_unit_keys: tuple[str, ...] = ()
    confirmed_context: tuple[ConfirmedUnitContext, ...] = ()
    reopened_unit_keys: tuple[str, ...] = ()
    immutable_unit_keys: tuple[str, ...] = ()
    requirement_brief_ref: str = ""
    requirement_brief_hash: str = ""
    base_unit_hash: str = ""
    user_feedback: str = ""
    schema_version: str = "unit-scope.v1"
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "unit-scope.v1":
            raise ValueError("unsupported unit scope schema")
        if self.purpose is not RunPurpose.PLAN_OUTLINE:
            if not self.outline_id or self.outline_version < 1 or not _is_hash(self.outline_hash):
                raise ValueError("unit scope requires a locked outline identity")
        if self.purpose in {RunPurpose.GENERATE_UNIT, RunPurpose.REVISE_UNIT}:
            if not self.current_unit_key or not self.current_unit_title or not self.section_node_keys:
                raise ValueError("generation scope requires exactly one current unit")
        elif self.current_unit_key:
            raise ValueError("outline and full review scopes cannot write a current unit")
        if self.purpose is RunPurpose.REVISE_UNIT:
            if (
                self.current_unit_key not in self.reopened_unit_keys
                or not self.user_feedback.strip()
                or not _is_hash(self.base_unit_hash)
            ):
                raise ValueError(
                    "revision scope requires reopened current unit, base hash and feedback"
                )
        if set(self.reopened_unit_keys) & set(self.immutable_unit_keys):
            raise ValueError("reopened and immutable unit sets must be disjoint")
        context_keys = [item.unit_key for item in self.confirmed_context]
        if len(context_keys) != len(set(context_keys)):
            raise ValueError("confirmed context unit keys must be unique")
        if not set(self.dependency_unit_keys).issubset(set(context_keys)):
            raise ValueError("all dependencies must have confirmed context")
        object.__setattr__(self, "scope_hash", _hash_json(self.payload()))

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "purpose": self.purpose.value,
            "outline_id": self.outline_id,
            "outline_version": self.outline_version,
            "outline_hash": _bare_hash(self.outline_hash),
            "current_unit_key": self.current_unit_key,
            "current_unit_title": self.current_unit_title,
            "current_unit_ordinal": self.current_unit_ordinal,
            "section_node_keys": list(self.section_node_keys),
            "dependency_unit_keys": list(self.dependency_unit_keys),
            "confirmed_context": [
                item.as_dict()
                for item in sorted(self.confirmed_context, key=lambda item: item.unit_key)
            ],
            "reopened_unit_keys": sorted(self.reopened_unit_keys),
            "immutable_unit_keys": sorted(self.immutable_unit_keys),
            "requirement_brief_ref": self.requirement_brief_ref,
            "requirement_brief_hash": _bare_hash(self.requirement_brief_hash),
            "base_unit_hash": _bare_hash(self.base_unit_hash),
            "user_feedback": self.user_feedback.strip(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.payload(), "scope_hash": self.scope_hash}


@dataclass(frozen=True)
class RequirementBrief:
    text: str
    required_content: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("requirement brief is empty")
        if any(not item.strip() for item in self.required_content):
            raise ValueError("required content contains an empty key")


@dataclass(frozen=True)
class LockedOutline:
    outline_id: str
    version: int
    candidate: OutlineCandidate

    def __post_init__(self) -> None:
        if not self.outline_id or self.version < 1:
            raise ValueError("locked outline requires identity and version")

    @property
    def content_hash(self) -> str:
        return self.candidate.content_hash


@dataclass(frozen=True)
class UnitResumeCursor:
    claim_generation: int = 0
    grounding_generation: int = 0
    quality_generation: int = 0

    def __post_init__(self) -> None:
        if min(self.claim_generation, self.grounding_generation, self.quality_generation) < 0:
            raise ValueError("unit resume generations cannot be negative")


@dataclass(frozen=True)
class UnitExecutionRequest:
    purpose: RunPurpose
    scope: UnitScope
    requirement_brief: RequirementBrief
    locked_outline: LockedOutline | None = None
    confirmed_context: tuple[ConfirmedUnitContext, ...] = ()
    resume: UnitResumeCursor | None = None
    base_candidate: UnitCandidate | None = None
    unresolved_unknown_ids: tuple[str, ...] = ()
    available_fact_ids: tuple[str, ...] = ()
    unresolved_conflict_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.purpose is not self.scope.purpose:
            raise ValueError("execution purpose and scope purpose mismatch")
        if self.purpose is not RunPurpose.PLAN_OUTLINE:
            if self.locked_outline is None:
                raise ValueError("scoped execution requires a locked outline")
            if (
                self.locked_outline.outline_id != self.scope.outline_id
                or self.locked_outline.version != self.scope.outline_version
                or self.locked_outline.content_hash != _bare_hash(self.scope.outline_hash)
            ):
                raise ValueError("locked outline does not match frozen scope")
        if self.confirmed_context and self.confirmed_context != self.scope.confirmed_context:
            raise ValueError("confirmed context does not match frozen scope")


@dataclass(frozen=True)
class UnitClaim:
    claim_type: ClaimType
    criticality: ClaimCriticality
    statement: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("unit claim statement is empty")

    def as_dict(self, unit_key: str) -> dict[str, object]:
        return {
            "unit_key": unit_key,
            "claim_type": self.claim_type.value,
            "criticality": self.criticality.value,
            "statement": self.statement.strip(),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UnitClaim":
        return cls(
            claim_type=ClaimType(str(value.get("claim_type", "PROPOSED_BEHAVIOR"))),
            criticality=ClaimCriticality(str(value.get("criticality", "IMPORTANT"))),
            statement=str(value.get("statement", "")),
            evidence_refs=_strings(value.get("evidence_refs")),
        )


@dataclass(frozen=True)
class UnitCandidate:
    unit_key: str
    title: str
    ordinal: int
    node_keys: tuple[str, ...]
    markdown: str
    claims: tuple[UnitClaim, ...] = ()
    claim_ids: tuple[str, ...] = ()
    unknown_ids: tuple[str, ...] = ()
    used_fact_ids: tuple[str, ...] = ()
    schema_version: str = "unit-candidate.v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "unit-candidate.v1":
            raise ValueError("unsupported unit candidate schema")
        if not self.unit_key or not self.title or self.ordinal < 0 or not self.node_keys:
            raise ValueError("unit candidate requires outline identity")
        if not self.markdown.strip():
            raise ValueError("unit candidate markdown is empty")
        object.__setattr__(self, "content_hash", _sha256(self.markdown.strip()))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "unit_key": self.unit_key,
            "title": self.title,
            "ordinal": self.ordinal,
            "node_keys": list(self.node_keys),
            "markdown": self.markdown.strip(),
            "content_hash": self.content_hash,
            "claims": [item.as_dict(self.unit_key) for item in self.claims],
            "claim_ids": list(self.claim_ids),
            "unknown_ids": list(self.unknown_ids),
            "used_fact_ids": list(self.used_fact_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UnitCandidate":
        candidate = cls(
            schema_version=str(value.get("schema_version", "unit-candidate.v1")),
            unit_key=str(value.get("unit_key", "")).strip(),
            title=str(value.get("title", "")).strip(),
            ordinal=int(value.get("ordinal", 0)),
            node_keys=_strings(value.get("node_keys")),
            markdown=str(value.get("markdown", "")),
            claims=tuple(
                UnitClaim.from_dict(item)
                for item in _mappings(value.get("claims", ()), "claims")
            ),
            claim_ids=_strings(value.get("claim_ids")),
            unknown_ids=_strings(value.get("unknown_ids")),
            used_fact_ids=_strings(value.get("used_fact_ids")),
        )
        supplied = str(value.get("content_hash", ""))
        if supplied and _bare_hash(supplied) != candidate.content_hash:
            raise ValueError("unit candidate content hash mismatch")
        return candidate


@dataclass(frozen=True)
class UnitPatch:
    unit_key: str
    base_content_hash: str
    replacement_markdown: str
    claims: tuple[UnitClaim, ...] = ()
    resolved_issue_ids: tuple[str, ...] = ()
    preserved_unknown_ids: tuple[str, ...] = ()
    used_fact_ids: tuple[str, ...] = ()
    schema_version: str = "unit-patch.v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != "unit-patch.v1" or not self.unit_key:
            raise ValueError("invalid unit patch identity")
        if not _is_hash(self.base_content_hash) or not self.replacement_markdown.strip():
            raise ValueError("unit patch requires base hash and replacement markdown")
        object.__setattr__(self, "content_hash", _sha256(self.replacement_markdown.strip()))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "unit_key": self.unit_key,
            "base_content_hash": _bare_hash(self.base_content_hash),
            "replacement_markdown": self.replacement_markdown.strip(),
            "content_hash": self.content_hash,
            "claims": [item.as_dict(self.unit_key) for item in self.claims],
            "resolved_issue_ids": list(self.resolved_issue_ids),
            "preserved_unknown_ids": list(self.preserved_unknown_ids),
            "used_fact_ids": list(self.used_fact_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "UnitPatch":
        patch = cls(
            schema_version=str(value.get("schema_version", "unit-patch.v1")),
            unit_key=str(value.get("unit_key", "")).strip(),
            base_content_hash=str(value.get("base_content_hash", "")),
            replacement_markdown=str(value.get("replacement_markdown", "")),
            claims=tuple(
                UnitClaim.from_dict(item)
                for item in _mappings(value.get("claims", ()), "claims")
            ),
            resolved_issue_ids=_strings(value.get("resolved_issue_ids")),
            preserved_unknown_ids=_strings(value.get("preserved_unknown_ids")),
            used_fact_ids=_strings(value.get("used_fact_ids")),
        )
        supplied = str(value.get("content_hash", ""))
        if supplied and _bare_hash(supplied) != patch.content_hash:
            raise ValueError("unit patch content hash mismatch")
        return patch


@dataclass(frozen=True)
class UnitRunRequest:
    purpose: RunPurpose
    scope: UnitScope
    task_message: str
    base_candidate: UnitCandidate | None = None
    unresolved_unknown_ids: tuple[str, ...] = ()
    available_fact_ids: tuple[str, ...] = ()
    grounding_findings: tuple[Mapping[str, object], ...] = ()
    unresolved_conflict_ids: tuple[str, ...] = ()
    required_content: tuple[str, ...] = ()
    locked_outline: OutlineCandidate | None = None
    resume: UnitResumeCursor | None = None

    def __post_init__(self) -> None:
        if self.purpose is not self.scope.purpose:
            raise ValueError("run purpose and unit scope purpose mismatch")
        if not self.task_message.strip():
            raise ValueError("unit run requires task message")
        if self.purpose is RunPurpose.REVISE_UNIT and self.base_candidate is None:
            raise ValueError("revision run requires a base candidate")


@dataclass(frozen=True)
class UnitRunResult:
    purpose: RunPurpose
    output_kind: RunOutputKind
    scope_hash: str
    content_hash: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if _OUTPUT_BY_PURPOSE[self.purpose] is not self.output_kind:
            raise ValueError("run output kind does not match purpose")
        if not _is_hash(self.scope_hash) or not _is_hash(self.content_hash):
            raise ValueError("run result requires scope and content hashes")


def _validate_outline(candidate: OutlineCandidate) -> None:
    if candidate.schema_version != "outline-candidate.v1":
        raise ValueError("unsupported outline candidate schema")
    if not candidate.title or not candidate.requirement_size or not candidate.nodes:
        raise ValueError("outline requires title, size and nodes")
    if not candidate.units or len(candidate.units) > 15:
        raise ValueError("outline requires between one and fifteen units")
    node_map = {item.node_key: item for item in candidate.nodes}
    unit_map = {item.unit_key: item for item in candidate.units}
    if len(node_map) != len(candidate.nodes) or "" in node_map:
        raise ValueError("outline node keys must be unique and non-empty")
    if len(unit_map) != len(candidate.units) or "" in unit_map:
        raise ValueError("outline unit keys must be unique and non-empty")
    assigned: set[str] = set()
    for node in candidate.nodes:
        if not node.title or node.ordinal < 0 or node.unit_key not in unit_map:
            raise ValueError("outline node has invalid identity or unit")
        depth = 1
        seen = {node.node_key}
        parent = node.parent_key
        while parent:
            if parent not in node_map or parent in seen:
                raise ValueError("outline contains a missing parent or node cycle")
            seen.add(parent)
            depth += 1
            if depth > 3:
                raise ValueError("outline depth exceeds three levels")
            parent = node_map[parent].parent_key
    for unit in candidate.units:
        if not unit.title or unit.ordinal < 0 or not unit.node_keys:
            raise ValueError("outline unit requires title, order and nodes")
        for key in unit.node_keys:
            if key not in node_map or node_map[key].unit_key != unit.unit_key or key in assigned:
                raise ValueError("outline node assignment is invalid")
            assigned.add(key)
        if any(dep not in unit_map or dep == unit.unit_key for dep in unit.depends_on):
            raise ValueError("outline unit dependency is invalid")
    if assigned != set(node_map):
        raise ValueError("every outline node must belong to exactly one unit")
    _validate_dependency_graph(candidate.units)


def _validate_dependency_graph(units: Sequence[OutlineUnit]) -> None:
    dependencies = {item.unit_key: set(item.depends_on) for item in units}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise ValueError("outline unit dependencies contain a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in dependencies[key]:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in dependencies:
        visit(key)


def _mappings(value: object, name: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list")
    result = tuple(item for item in value if isinstance(item, Mapping))
    if len(result) != len(value):
        raise ValueError(f"{name} entries must be objects")
    return result


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected a list of strings")
    items = tuple(str(item).strip() for item in value)
    if any(not item for item in items) or len(items) != len(set(items)):
        raise ValueError("string list must contain unique non-empty values")
    return items


def _hash_json(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256(encoded)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bare_hash(value: str) -> str:
    return value.removeprefix("sha256:")


def _is_hash(value: str) -> bool:
    raw = _bare_hash(value)
    return len(raw) == 64 and all(char in "0123456789abcdef" for char in raw.lower())
