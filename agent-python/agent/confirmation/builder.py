from __future__ import annotations

import hashlib

from agent.draft import DraftBundle


def build_confirmation_units(
    bundle: DraftBundle,
    *,
    immutable_unit_keys: tuple[str, ...] = (),
) -> tuple[dict[str, object], ...]:
    immutable = frozenset(immutable_unit_keys)
    claims_by_unit: dict[str, list[str]] = {}
    unknowns_by_unit: dict[str, list[str]] = {}
    for claim in bundle.claims:
        claims_by_unit.setdefault(claim.unit_key, []).append(claim.claim_id)
    for unknown in bundle.unknowns:
        unknowns_by_unit.setdefault(unknown.unit_key, []).append(unknown.unknown_id)
    result = []
    for unit in bundle.units:
        content_hash = hashlib.sha256(unit.markdown.encode("utf-8")).hexdigest()
        result.append(
            {
                "unit_key": unit.unit_key,
                "title": unit.title,
                "order": unit.order,
                "markdown": unit.markdown,
                "content_hash": content_hash,
                "claim_ids": claims_by_unit.get(unit.unit_key, []),
                "unknown_ids": unknowns_by_unit.get(unit.unit_key, []),
                "depends_on": list(unit.depends_on),
                "immutable": unit.unit_key in immutable,
                "confirmation_status": "PENDING",
            }
        )
    return tuple(result)
