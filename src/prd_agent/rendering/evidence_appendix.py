from __future__ import annotations


def _locator(reference) -> str:
    location = reference.path
    if reference.line_start is not None:
        location += f":{reference.line_start}"
        if (
            reference.line_end is not None
            and reference.line_end != reference.line_start
        ):
            location += f"-{reference.line_end}"
    elif reference.symbol:
        location += f"#{reference.symbol}"
    return location


def render_evidence_appendix(grounding_results) -> str:
    """Render auditable locators only; source excerpts never enter the document."""

    references = sorted(
        (
            reference
            for result in grounding_results
            for reference in result.references
        ),
        key=lambda item: (
            item.path,
            item.line_start or 0,
            item.fact_id,
            item.evidence_id,
        ),
    )
    if not references:
        return ""
    lines = ["## 参考依据", ""]
    for reference in references:
        claims = ", ".join(reference.claim_ids) or "未关联 Claim"
        lines.extend(
            [
                f"- Claim: {claims}",
                f"  - Fact: {reference.fact_id}",
                f"  - Source: {_locator(reference)}",
                f"  - Commit: {reference.resolved_commit_sha[:12]}",
                "  - Verification: SUPPORTED",
            ]
        )
    return "\n".join(lines) + "\n"
