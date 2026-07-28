from __future__ import annotations

from prd_agent.domain.enums import SectionStatus


def build_confirmed_context(unit, sections, *, max_content_chars: int = 4000):
    """Return only latest confirmed sections from direct unit dependencies."""

    latest = {}
    dependencies = set(unit.depends_on_unit_ids)
    for section in sections:
        if (
            section.unit_id not in dependencies
            or section.status != SectionStatus.CONFIRMED
        ):
            continue
        key = section.node_id or section.unit_id
        current = latest.get(key)
        if current is None or section.version > current.version:
            latest[key] = section
    return tuple(
        {
            "section_id": section.section_id,
            "unit_id": section.unit_id,
            "node_id": section.node_id,
            "version": section.version,
            "title": section.title,
            "content": section.content[:max_content_chars],
            "content_hash": section.content_hash,
        }
        for section in sorted(
            latest.values(),
            key=lambda item: (item.unit_id, item.node_id or "", item.version),
        )
    )
