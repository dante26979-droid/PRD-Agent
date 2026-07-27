from __future__ import annotations

import re

from prd_agent.hashing import sha256_json

from .models import ExportBlock, ExportBlockType, ExportDocument


_ORDERED = re.compile(r"^\d+[.)]\s+(.+)$")
_TABLE_SEPARATOR = re.compile(
    r"^\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$"
)


class FeishuExportFormatter:
    """Pure deterministic Markdown-to-export-block transformation."""

    def format(
        self,
        markdown: str,
        *,
        document_version: int,
        unresolved_items: tuple[str, ...] = (),
    ) -> ExportDocument:
        normalized = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
        blocks = tuple(self._parse(normalized.splitlines()))
        title = next(
            (
                block.text
                for block in blocks
                if block.block_type == ExportBlockType.HEADING
                and block.level == 1
                and block.text
            ),
            "PRD",
        )
        canonical = {
            "title": title,
            "document_version": document_version,
            "blocks": [block.model_dump(mode="json") for block in blocks],
            "unresolved_items": list(unresolved_items),
        }
        return ExportDocument(
            **canonical,
            content_hash=sha256_json(canonical),
        )

    def _parse(self, lines: list[str]) -> list[ExportBlock]:
        blocks: list[ExportBlock] = []
        index = 0
        while index < len(lines):
            raw = lines[index]
            line = raw.strip()
            if not line:
                index += 1
                continue
            if line.startswith("```"):
                block, index = self._code(lines, index)
                blocks.append(block)
                continue
            heading = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading:
                level = min(len(heading.group(1)), 3)
                blocks.append(
                    ExportBlock(
                        block_type=ExportBlockType.HEADING,
                        level=level,
                        text=heading.group(2).strip(),
                    )
                )
                index += 1
                continue
            if line in {"---", "***", "___"}:
                blocks.append(ExportBlock(block_type=ExportBlockType.DIVIDER))
                index += 1
                continue
            if line.startswith(">"):
                values = []
                while index < len(lines) and lines[index].strip().startswith(">"):
                    values.append(lines[index].strip()[1:].strip())
                    index += 1
                blocks.append(
                    ExportBlock(
                        block_type=ExportBlockType.QUOTE,
                        text="\n".join(values),
                    )
                )
                continue
            if line.startswith(("- ", "* ", "+ ")):
                values = []
                while index < len(lines):
                    current = lines[index].strip()
                    if not current.startswith(("- ", "* ", "+ ")):
                        break
                    values.append(current[2:].strip())
                    index += 1
                blocks.append(
                    ExportBlock(
                        block_type=ExportBlockType.UNORDERED_LIST,
                        items=tuple(values),
                    )
                )
                continue
            ordered = _ORDERED.match(line)
            if ordered:
                values = []
                while index < len(lines):
                    current = _ORDERED.match(lines[index].strip())
                    if current is None:
                        break
                    values.append(current.group(1).strip())
                    index += 1
                blocks.append(
                    ExportBlock(
                        block_type=ExportBlockType.ORDERED_LIST,
                        items=tuple(values),
                    )
                )
                continue
            if (
                line.startswith("|")
                and index + 1 < len(lines)
                and _TABLE_SEPARATOR.match(lines[index + 1].strip())
            ):
                block, index = self._table(lines, index)
                blocks.append(block)
                continue
            values = [line]
            index += 1
            while index < len(lines):
                current = lines[index].strip()
                if not current or self._starts_block(lines, index):
                    break
                values.append(current)
                index += 1
            blocks.append(
                ExportBlock(
                    block_type=ExportBlockType.PARAGRAPH,
                    text=" ".join(values),
                )
            )
        return blocks

    @staticmethod
    def _starts_block(lines: list[str], index: int) -> bool:
        line = lines[index].strip()
        return bool(
            line.startswith(("#", ">", "```", "- ", "* ", "+ "))
            or _ORDERED.match(line)
            or line in {"---", "***", "___"}
            or (
                line.startswith("|")
                and index + 1 < len(lines)
                and _TABLE_SEPARATOR.match(lines[index + 1].strip())
            )
        )

    @staticmethod
    def _code(lines: list[str], index: int) -> tuple[ExportBlock, int]:
        language = lines[index].strip()[3:].strip() or None
        index += 1
        values = []
        while index < len(lines) and not lines[index].strip().startswith("```"):
            values.append(lines[index])
            index += 1
        if index < len(lines):
            index += 1
        return (
            ExportBlock(
                block_type=ExportBlockType.CODE,
                text="\n".join(values),
                language=language,
            ),
            index,
        )

    @staticmethod
    def _table(lines: list[str], index: int) -> tuple[ExportBlock, int]:
        rows = [FeishuExportFormatter._table_row(lines[index])]
        index += 2
        while index < len(lines) and lines[index].strip().startswith("|"):
            rows.append(FeishuExportFormatter._table_row(lines[index]))
            index += 1
        return (
            ExportBlock(block_type=ExportBlockType.TABLE, rows=tuple(rows)),
            index,
        )

    @staticmethod
    def _table_row(line: str) -> tuple[str, ...]:
        return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
