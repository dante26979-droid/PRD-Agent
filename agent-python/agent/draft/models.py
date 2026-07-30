from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence


class ClaimType(StrEnum):
    USER_REQUIREMENT = "USER_REQUIREMENT"
    CURRENT_STATE = "CURRENT_STATE"
    CONSTRAINT = "CONSTRAINT"
    PROPOSED_BEHAVIOR = "PROPOSED_BEHAVIOR"
    ACCEPTANCE_CRITERION = "ACCEPTANCE_CRITERION"
    ASSUMPTION = "ASSUMPTION"


class ClaimCriticality(StrEnum):
    BLOCKING = "BLOCKING"
    IMPORTANT = "IMPORTANT"
    INFORMATIONAL = "INFORMATIONAL"


@dataclass(frozen=True)
class Claim:
    claim_id: str
    unit_key: str
    claim_type: ClaimType
    criticality: ClaimCriticality
    statement: str
    evidence_refs: tuple[str, ...] = ()
    requirement_refs: tuple[str, ...] = ()
    trace_to_claim_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "claim_id": self.claim_id,
            "unit_key": self.unit_key,
            "claim_type": self.claim_type.value,
            "criticality": self.criticality.value,
            "statement": self.statement,
            "evidence_refs": list(self.evidence_refs),
            "requirement_refs": list(self.requirement_refs),
            "trace_to_claim_ids": list(self.trace_to_claim_ids),
        }


@dataclass(frozen=True)
class Unknown:
    unknown_id: str
    unit_key: str
    statement: str
    reason_code: str
    related_claim_ids: tuple[str, ...]
    required_user_input: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "unknown_id": self.unknown_id,
            "unit_key": self.unit_key,
            "statement": self.statement,
            "reason_code": self.reason_code,
            "related_claim_ids": list(self.related_claim_ids),
            "required_user_input": self.required_user_input,
        }


@dataclass(frozen=True)
class DraftUnit:
    unit_key: str
    title: str
    markdown: str
    order: int
    depends_on: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_key": self.unit_key,
            "title": self.title,
            "markdown": self.markdown,
            "order": self.order,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class DraftBundle:
    schema_version: str
    generation: int
    units: tuple[DraftUnit, ...]
    claims: tuple[Claim, ...]
    unknowns: tuple[Unknown, ...]
    markdown: str

    @classmethod
    def build(
        cls,
        *,
        run_id: str,
        task_id: str,
        task_version: int,
        generation: int,
        markdown: str,
        structured: Mapping[str, object] | None = None,
    ) -> "DraftBundle":
        units = _units(structured, markdown)
        requirement_ref = _hash("\x00".join((task_id, str(task_version))))
        claims = _claims(
            structured,
            run_id=run_id,
            generation=generation,
            units=units,
            requirement_ref=requirement_ref,
        )
        return cls(
            schema_version="draft-bundle.v1",
            generation=generation,
            units=units,
            claims=claims,
            unknowns=(),
            markdown=_render(units),
        )

    def with_unknowns(self, unknowns: Sequence[Unknown]) -> "DraftBundle":
        if not unknowns:
            return self
        appendix = "\n\n## 待确认与未知项\n\n" + "\n".join(
            f"- {item.statement}" for item in unknowns
        )
        units = self.units + (
            DraftUnit(
                unit_key="unknowns",
                title="待确认与未知项",
                markdown=appendix.strip(),
                order=(max((item.order for item in self.units), default=0) + 10),
            ),
        )
        return DraftBundle(
            schema_version=self.schema_version,
            generation=self.generation,
            units=units,
            claims=self.claims,
            unknowns=tuple(unknowns),
            markdown=_render(units),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "units": [item.as_dict() for item in self.units],
            "claims": [item.as_dict() for item in self.claims],
            "unknowns": [item.as_dict() for item in self.unknowns],
            "markdown": self.markdown,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "DraftBundle":
        units = tuple(
            DraftUnit(
                unit_key=str(item["unit_key"]),
                title=str(item["title"]),
                markdown=str(item["markdown"]),
                order=int(item["order"]),
                depends_on=tuple(str(value) for value in item.get("depends_on", ())),
            )
            for item in _object_list(raw.get("units"), "units")
        )
        claims = tuple(
            Claim(
                claim_id=str(item["claim_id"]),
                unit_key=str(item["unit_key"]),
                claim_type=ClaimType(str(item["claim_type"])),
                criticality=ClaimCriticality(str(item["criticality"])),
                statement=str(item["statement"]),
                evidence_refs=tuple(
                    str(value) for value in item.get("evidence_refs", ())
                ),
                requirement_refs=tuple(
                    str(value) for value in item.get("requirement_refs", ())
                ),
                trace_to_claim_ids=tuple(
                    str(value) for value in item.get("trace_to_claim_ids", ())
                ),
            )
            for item in _object_list(raw.get("claims"), "claims")
        )
        unknowns = tuple(
            Unknown(
                unknown_id=str(item["unknown_id"]),
                unit_key=str(item["unit_key"]),
                statement=str(item["statement"]),
                reason_code=str(item["reason_code"]),
                related_claim_ids=tuple(
                    str(value) for value in item.get("related_claim_ids", ())
                ),
                required_user_input=(
                    str(item["required_user_input"])
                    if item.get("required_user_input") is not None
                    else None
                ),
            )
            for item in _object_list(raw.get("unknowns"), "unknowns")
        )
        return cls(
            schema_version=str(raw.get("schema_version", "draft-bundle.v1")),
            generation=int(raw["generation"]),
            units=units,
            claims=claims,
            unknowns=unknowns,
            markdown=str(raw["markdown"]),
        )


def _units(
    structured: Mapping[str, object] | None,
    markdown: str,
) -> tuple[DraftUnit, ...]:
    raw_units = structured.get("units") if structured else None
    if isinstance(raw_units, list) and raw_units:
        result = []
        seen = set()
        for index, raw in enumerate(raw_units):
            if not isinstance(raw, Mapping):
                raise ValueError("draft unit must be an object")
            title = str(raw.get("title", "")).strip()
            content = str(raw.get("markdown", "")).strip()
            key = _unit_key(str(raw.get("unit_key", "")) or title)
            if not title or not content or key in seen:
                raise ValueError("draft unit requires unique key, title and markdown")
            seen.add(key)
            result.append(
                DraftUnit(
                    unit_key=key,
                    title=title,
                    markdown=content,
                    order=int(raw.get("order", (index + 1) * 10)),
                    depends_on=tuple(str(item) for item in raw.get("depends_on", ())),
                )
            )
        return tuple(sorted(result, key=lambda item: item.order))
    return _split_markdown(markdown)


def _split_markdown(markdown: str) -> tuple[DraftUnit, ...]:
    matches = list(re.finditer(r"(?m)^##\s+(.+)$", markdown))
    if not matches:
        title = markdown.splitlines()[0].lstrip("# ").strip() or "PRD"
        return (DraftUnit(_unit_key(title), title, markdown.strip(), 10),)
    result = []
    prefix = markdown[: matches[0].start()].strip()
    if prefix:
        result.append(DraftUnit("overview", "概述", prefix, 10))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        title = match.group(1).strip()
        result.append(
            DraftUnit(
                _unit_key(title),
                title,
                markdown[match.start() : end].strip(),
                (len(result) + 1) * 10,
            )
        )
    return tuple(result)


def _claims(
    structured: Mapping[str, object] | None,
    *,
    run_id: str,
    generation: int,
    units: tuple[DraftUnit, ...],
    requirement_ref: str,
) -> tuple[Claim, ...]:
    raw_claims = structured.get("claims") if structured else None
    unit_keys = {item.unit_key for item in units}
    if isinstance(raw_claims, list):
        result = []
        for raw in raw_claims:
            if not isinstance(raw, Mapping):
                raise ValueError("claim must be an object")
            unit_key = _unit_key(str(raw.get("unit_key", "")))
            statement = str(raw.get("statement", "")).strip()
            if unit_key not in unit_keys or not statement:
                raise ValueError("claim references an unknown unit")
            claim_type = ClaimType(str(raw.get("claim_type", "PROPOSED_BEHAVIOR")))
            criticality = ClaimCriticality(
                str(raw.get("criticality", "IMPORTANT"))
            )
            result.append(
                Claim(
                    claim_id=_claim_id(
                        run_id, generation, unit_key, claim_type, statement
                    ),
                    unit_key=unit_key,
                    claim_type=claim_type,
                    criticality=criticality,
                    statement=statement,
                    evidence_refs=tuple(
                        str(item) for item in raw.get("evidence_refs", ())
                    ),
                    requirement_refs=tuple(
                        str(item) for item in raw.get("requirement_refs", ())
                    ),
                    trace_to_claim_ids=tuple(
                        str(item) for item in raw.get("trace_to_claim_ids", ())
                    ),
                )
            )
        return tuple(result)
    return tuple(
        Claim(
            claim_id=_claim_id(
                run_id,
                generation,
                unit.unit_key,
                ClaimType.PROPOSED_BEHAVIOR,
                unit.title,
            ),
            unit_key=unit.unit_key,
            claim_type=ClaimType.PROPOSED_BEHAVIOR,
            criticality=ClaimCriticality.IMPORTANT,
            statement=unit.title,
            requirement_refs=(requirement_ref,),
        )
        for unit in units
    )


def _render(units: Sequence[DraftUnit]) -> str:
    return "\n\n".join(item.markdown.strip() for item in units if item.markdown.strip())


def _unit_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "unit-" + _hash(value)[:12]


def _claim_id(
    run_id: str,
    generation: int,
    unit_key: str,
    claim_type: ClaimType,
    statement: str,
) -> str:
    return "claim-" + _hash(
        "\x00".join(
            (run_id, str(generation), unit_key, claim_type.value, statement.strip())
        )
    )[:24]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _object_list(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise ValueError(f"draft bundle {field} must be an array")
    if not all(isinstance(item, Mapping) for item in value):
        raise ValueError(f"draft bundle {field} must contain objects")
    return value
