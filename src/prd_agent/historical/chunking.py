from __future__ import annotations

import hashlib
import re

from prd_agent.hashing import canonical_json

from .models import HistoricalPrdChunk
from .text import token_text

_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")


def _digest(value: object, *, prefix: str) -> str:
    raw = canonical_json(value).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:32]}"


def content_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sections(markdown: str) -> tuple[tuple[tuple[str, ...], str], ...]:
    paths: list[str] = []
    current_path: tuple[str, ...] = ()
    lines: list[str] = []
    result: list[tuple[tuple[str, ...], str]] = []

    def flush() -> None:
        content = "\n".join(lines).strip()
        if content:
            result.append((current_path, content))

    for line in markdown.splitlines():
        heading = _HEADING.match(line)
        if not heading:
            lines.append(line)
            continue
        flush()
        lines.clear()
        level = len(heading.group(1))
        title = heading.group(2).strip()
        paths[level - 1 :] = []
        while len(paths) < level - 1:
            paths.append("")
        paths.append(title)
        current_path = tuple(item for item in paths if item)
    flush()
    return tuple(result)


def _hard_split(value: str, hard_limit: int) -> tuple[str, ...]:
    result: list[str] = []
    remaining = value.strip()
    while len(remaining) > hard_limit:
        boundary = max(
            remaining.rfind(mark, 0, hard_limit + 1)
            for mark in ("\n", "。", "！", "？", ".", ";", "；")
        )
        if boundary < hard_limit // 2:
            boundary = hard_limit
        else:
            boundary += 1
        result.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        result.append(remaining)
    return tuple(result)


def _chunk_section(
    content: str,
    *,
    target_limit: int,
    hard_limit: int,
) -> tuple[str, ...]:
    blocks = [item.strip() for item in re.split(r"\n\s*\n", content) if item.strip()]
    chunks: list[str] = []
    current = ""
    for original in blocks:
        for block in _hard_split(original, hard_limit):
            candidate = f"{current}\n\n{block}".strip() if current else block
            if current and len(candidate) > target_limit:
                chunks.append(current)
                current = block
            else:
                current = candidate
            if len(current) >= hard_limit:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return tuple(chunks)


def chunk_markdown(
    markdown: str,
    *,
    document_version_id: str,
    title: str,
    product_tags: tuple[str, ...] = (),
    target_limit: int = 600,
    hard_limit: int = 1200,
) -> tuple[HistoricalPrdChunk, ...]:
    if not 1 <= target_limit <= hard_limit <= 1200:
        raise ValueError("chunk limits must satisfy 1 <= target <= hard <= 1200")
    chunks: list[HistoricalPrdChunk] = []
    ordinal = 0
    for section_path, section_content in _sections(markdown):
        for content in _chunk_section(
            section_content,
            target_limit=target_limit,
            hard_limit=hard_limit,
        ):
            hashed = content_hash(content)
            indexed_text, count = token_text(
                content,
                title=title,
                product_tags=product_tags,
            )
            chunks.append(
                HistoricalPrdChunk(
                    chunk_id=_digest(
                        {
                            "document_version_id": document_version_id,
                            "section_path": section_path,
                            "ordinal": ordinal,
                            "content_hash": hashed,
                        },
                        prefix="chunk",
                    ),
                    document_version_id=document_version_id,
                    section_path=section_path,
                    ordinal=ordinal,
                    content=content,
                    content_hash=hashed,
                    token_text=indexed_text,
                    token_count=count,
                )
            )
            ordinal += 1
    return tuple(chunks)
