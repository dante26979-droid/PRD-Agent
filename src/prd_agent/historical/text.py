from __future__ import annotations

import re
import unicodedata

from prd_agent.repository.content_policy import RepositoryContentPolicy

TOKENIZER_VERSION = "historical-tokenizer.zh-bigram.v1"

_DANGEROUS_BLOCK = re.compile(
    r"(?is)<(script|style|iframe|form)\b[^>]*>.*?</\1\s*>"
)
_HTML_TAG = re.compile(r"(?s)<[^>]+>")
_ASCII_WORD_OR_CJK = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")
_DEFAULT_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "or",
        "the",
        "to",
        "of",
        "in",
        "is",
        "为",
        "与",
        "和",
        "的",
        "了",
    }
)


class HistoricalContentError(ValueError):
    pass


def normalize_markdown(value: str) -> str:
    if "\x00" in value:
        raise HistoricalContentError("binary content is not allowed")
    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _DANGEROUS_BLOCK.sub("", normalized)
    normalized = _HTML_TAG.sub("", normalized)
    lines = [line.rstrip() for line in normalized.splitlines()]
    return "\n".join(lines).strip()


def tokenize(
    value: str,
    *,
    stopwords: frozenset[str] = _DEFAULT_STOPWORDS,
) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFC", value).lower()
    tokens: list[str] = []
    for match in _ASCII_WORD_OR_CJK.finditer(normalized):
        part = match.group(0)
        if part.isascii():
            if part not in stopwords:
                tokens.append(part)
            continue
        characters = tuple(part)
        tokens.extend(character for character in characters if character not in stopwords)
        tokens.extend(
            "".join(characters[index : index + 2])
            for index in range(len(characters) - 1)
        )
    return tuple(tokens)


def token_text(
    content: str,
    *,
    title: str = "",
    product_tags: tuple[str, ...] = (),
) -> tuple[str, int]:
    content_tokens = tokenize(content)
    weighted = (
        content_tokens
        + tokenize(title)
        + tokenize(title)
        + tokenize(" ".join(product_tags))
        + tokenize(" ".join(product_tags))
    )
    return " ".join(weighted), len(weighted)


def safe_excerpt(value: str, *, limit: int = 800) -> tuple[str, bool]:
    redacted, changed = RepositoryContentPolicy().redact(value)
    compact = re.sub(r"\s+", " ", redacted).strip()
    if len(compact) > limit:
        compact = compact[: max(0, limit - 1)].rstrip() + "…"
    return compact, changed
