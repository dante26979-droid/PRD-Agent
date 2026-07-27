from __future__ import annotations

from prd_agent.domain.entities import OutlineVersion, PrdSectionVersion, RequirementBrief
from prd_agent.domain.enums import OutlineStatus, UnitStatus
from prd_agent.domain.errors import InvalidTransition


def _list(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- 待确认"


def render_markdown(
    brief: RequirementBrief,
    outline: OutlineVersion,
    section: PrdSectionVersion | tuple[PrdSectionVersion, ...],
    evidence_appendix: str = "",
) -> str:
    """Render only immutable confirmed inputs; drafts can never leak into output."""

    if outline.status != OutlineStatus.CONFIRMED:
        raise InvalidTransition("cannot render an unconfirmed outline")
    sections = section if isinstance(section, tuple) else (section,)
    for item in sections:
        unit = next(
            (unit for unit in outline.confirmation_units if unit.unit_id == item.unit_id),
            None,
        )
        if not unit or unit.status != UnitStatus.CONFIRMED:
            raise InvalidTransition("cannot render an unconfirmed unit")
    problem = brief.problem or "待确认"
    body = (
        f"# {outline.title}\n\n"
        f"## 需求摘要\n\n{problem}\n\n"
        f"## 范围边界\n\n"
        f"### 范围内\n\n{_list(brief.scope_in)}\n\n"
        f"### 范围外\n\n{_list(brief.scope_out)}\n\n"
        f"## 业务规则\n\n{_list(brief.product_rules)}\n\n"
    )
    for item in sections:
        heading = (
            f"第一确认单元：{item.title}" if len(sections) == 1 else item.title
        )
        body += f"## {heading}\n\n{item.content}\n\n"
    body += f"## 待确认事项\n\n{_list(brief.open_questions)}\n"
    if evidence_appendix:
        body += "\n" + evidence_appendix
    return body
