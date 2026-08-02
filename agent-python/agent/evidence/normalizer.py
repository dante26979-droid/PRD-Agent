from __future__ import annotations

import hashlib

from agent.v1 import agent_execution_pb2 as proto


def repository_hits_to_evidence(
    binding_id: str,
    revision: str,
    hits,
    *,
    limit: int = 100,
    max_excerpt_chars: int = 4000,
) -> tuple[proto.EvidenceItem, ...]:
    if not binding_id or not revision:
        raise ValueError("repository binding and revision are required")
    items = []
    seen = set()
    for hit in hits:
        excerpt = hit.snippet[:max_excerpt_chars]
        locator = f"github://{binding_id}@{revision}/{hit.path}#L{hit.line}"
        key = (locator, excerpt)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            proto.EvidenceItem(
                source_type="github",
                source_id=binding_id,
                locator=locator,
                excerpt_hash="sha256:"
                + hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                excerpt=excerpt,
                source_kind="github",
                binding_id=binding_id,
                source_version=revision,
                outcome_kind="HIT",
            )
        )
        if len(items) >= limit:
            break
    return tuple(items)
