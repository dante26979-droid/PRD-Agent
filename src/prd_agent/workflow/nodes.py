from __future__ import annotations

from typing import Any, Callable, Mapping, TypeVar

from prd_agent.domain.entities import RequirementBrief
from prd_agent.domain.errors import ModelOutputError
from prd_agent.grounding.models import PrdClaim
from prd_agent.workflow.model import WorkflowModel


T = TypeVar("T")


def invoke_validated(
    model: WorkflowModel,
    operation: str,
    payload: Mapping[str, Any],
    validator: Callable[[Mapping[str, Any]], T],
) -> T:
    """Validate model output and allow exactly one same-input format repair."""

    first_error: Exception | None = None
    for repair in (False, True):
        result = model.complete(operation, payload, repair=repair)
        try:
            return validator(result.structured_output)
        except (KeyError, TypeError, ValueError, ModelOutputError) as exc:
            first_error = exc
    raise ModelOutputError(f"invalid {operation} output after one repair: {first_error}")


def extract_requirement_brief(
    model: WorkflowModel,
    message: str,
    previous_brief: RequirementBrief | None = None,
) -> RequirementBrief:
    payload: dict[str, Any] = {"user_message": message}
    if previous_brief:
        payload["previous_brief"] = previous_brief.as_dict()
    return invoke_validated(model, "extract_requirement_brief", payload, RequirementBrief.from_mapping)


def validate_outline(value: Mapping[str, Any]) -> dict[str, Any]:
    title = str(value.get("title", "")).strip()
    nodes = value.get("nodes")
    if not title or not isinstance(nodes, list) or not nodes:
        raise ModelOutputError("outline requires a title and at least one node")
    normalized_nodes: list[dict[str, Any]] = []
    node_keys: set[str] = set()
    node_levels: dict[str, int] = {}
    for index, node in enumerate(nodes, start=1):
        if not isinstance(node, Mapping):
            raise ModelOutputError("outline node must be an object")
        required = ("title", "purpose", "complexity")
        if any(not str(node.get(name, "")).strip() for name in required):
            raise ModelOutputError("outline node requires title, purpose and complexity")
        key = str(node.get("key") or f"node-{index}").strip()
        if not key or key in node_keys:
            raise ModelOutputError("outline node keys must be unique")
        parent_key_value = node.get("parent_key")
        parent_key = (
            str(parent_key_value).strip()
            if parent_key_value is not None
            else None
        )
        if parent_key and parent_key not in node_keys:
            raise ModelOutputError("outline parent must precede its child")
        inferred_level = node_levels[parent_key] + 1 if parent_key else 1
        level = int(node.get("level", inferred_level))
        if level != inferred_level or level < 1 or level > 3:
            raise ModelOutputError("outline node level must match a maximum three-level tree")
        node_keys.add(key)
        node_levels[key] = level
        normalized_nodes.append(
            {
                **dict(node),
                "key": key,
                "parent_key": parent_key,
                "level": level,
            }
        )

    units = value.get("units")
    if units is None:
        units = [
            {
                "key": f"unit-{index}",
                "title": node["title"],
                "node_keys": [node["key"]],
                "depends_on_unit_keys": [] if index == 1 else [f"unit-{index - 1}"],
            }
            for index, node in enumerate(normalized_nodes, start=1)
        ]
    if not isinstance(units, list) or not units or len(units) > 15:
        raise ModelOutputError("outline requires between 1 and 15 confirmation units")
    normalized_units: list[dict[str, Any]] = []
    unit_keys: set[str] = set()
    covered_nodes: list[str] = []
    for index, unit in enumerate(units, start=1):
        if not isinstance(unit, Mapping):
            raise ModelOutputError("confirmation unit must be an object")
        key = str(unit.get("key") or f"unit-{index}").strip()
        title_value = str(unit.get("title", "")).strip()
        mapped = tuple(str(item) for item in unit.get("node_keys", ()))
        dependencies = tuple(str(item) for item in unit.get("depends_on_unit_keys", ()))
        if not key or key in unit_keys or not title_value or not mapped:
            raise ModelOutputError("confirmation unit requires unique key, title and nodes")
        if any(item not in node_keys for item in mapped):
            raise ModelOutputError("confirmation unit references an unknown node")
        if any(item not in unit_keys for item in dependencies):
            raise ModelOutputError("confirmation unit dependencies must precede the unit")
        unit_keys.add(key)
        covered_nodes.extend(mapped)
        normalized_units.append(
            {
                **dict(unit),
                "key": key,
                "title": title_value,
                "node_keys": mapped,
                "depends_on_unit_keys": dependencies,
            }
        )
    if sorted(covered_nodes) != sorted(node_keys) or len(covered_nodes) != len(node_keys):
        raise ModelOutputError("every outline node must belong to exactly one unit")
    return {"title": title, "nodes": normalized_nodes, "units": normalized_units}


def validate_unit_content(value: Mapping[str, Any]) -> str:
    content = str(value.get("content", "")).strip()
    if not content:
        raise ModelOutputError("confirmation unit requires non-empty Markdown content")
    return content


def validate_unit_draft(value: Mapping[str, Any]) -> dict[str, Any]:
    raw_sections = value.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ModelOutputError("confirmation unit sections must be an array")
    sections: list[dict[str, str]] = []
    for item in raw_sections:
        if not isinstance(item, Mapping):
            raise ModelOutputError("confirmation unit section must be an object")
        node_key = str(item.get("node_key", "")).strip()
        title = str(item.get("title", "")).strip()
        section_content = str(item.get("content", "")).strip()
        if not node_key or not title or not section_content:
            raise ModelOutputError(
                "confirmation unit section requires node_key, title and content"
            )
        sections.append(
            {
                "node_key": node_key,
                "title": title,
                "content": section_content,
            }
        )
    if sections:
        content = "\n\n".join(
            f"### {item['title']}\n\n{item['content']}" for item in sections
        )
    else:
        content = validate_unit_content(value)
    raw_claims = value.get("claims", [])
    if not isinstance(raw_claims, list):
        raise ModelOutputError("confirmation unit claims must be an array")
    try:
        claims = tuple(PrdClaim.model_validate(item) for item in raw_claims)
    except (TypeError, ValueError) as exc:
        raise ModelOutputError(f"invalid confirmation unit claims: {exc}") from exc
    return {"content": content, "claims": claims, "sections": tuple(sections)}
