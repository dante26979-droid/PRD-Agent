from __future__ import annotations

import re
from typing import Any

from .errors import BinaryFileBlocked, FileTooLarge


class RepositoryContentPolicy:
    _secret_patterns = (
        re.compile(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*['\"]?[^\s'\"]+"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"(?i)postgres(?:ql)?://[^\s]+"),
        re.compile(
            r"(?i)\b(password|token|api[_-]?key|secret)\b"
            r"(\s+[A-Z][A-Z0-9_]*(?:\([^)]*\))?)*"
            r"\s+DEFAULT\s+['\"]?[^\s,'\")]+['\"]?"
        ),
    )
    _sensitive_identifier = re.compile(
        r"(?i)(password|passwd|token|api[_-]?key|secret|credential)"
    )
    _sensitive_value_keys = frozenset(
        {"default", "example", "examples", "value", "enum", "pattern"}
    )
    _sensitive_block_value = re.compile(
        r"(?i)\b(default|example|examples|value|enum|pattern)\s*:"
        r"\s*(\[[^\]\n]*\]|[^\n]+)"
    )

    def decode_text(self, data: bytes, *, max_bytes: int) -> str:
        if len(data) > max_bytes:
            raise FileTooLarge("file exceeds repository content limit")
        if b"\x00" in data:
            raise BinaryFileBlocked("binary file is blocked")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BinaryFileBlocked("non UTF-8 file is blocked") from exc

    def redact(self, value: str) -> tuple[str, bool]:
        redacted = value
        changed = False
        for pattern in self._secret_patterns:
            updated, count = pattern.subn("[REDACTED]", redacted)
            redacted = updated
            changed = changed or count > 0
        if self._sensitive_identifier.search(value):
            redacted, count = self._sensitive_block_value.subn(
                "[REDACTED]", redacted
            )
            changed = changed or count > 0
        return redacted, changed

    def redact_structure(self, value: Any) -> tuple[Any, bool]:
        """Recursively sanitize parser metadata at the single public boundary."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, tuple):
            changed = False
            output = []
            for item in value:
                sanitized, item_changed = self.redact_structure(item)
                output.append(sanitized)
                changed = changed or item_changed
            return tuple(output), changed
        if isinstance(value, list):
            changed = False
            output = []
            for item in value:
                sanitized, item_changed = self.redact_structure(item)
                output.append(sanitized)
                changed = changed or item_changed
            return output, changed
        if isinstance(value, dict):
            sensitive_context = any(
                isinstance(item, str)
                and self._sensitive_identifier.search(item)
                for key, item in value.items()
                if str(key).lower() in {"field", "column", "name", "parameter"}
            )
            changed = False
            output = {}
            for key, item in value.items():
                if (
                    sensitive_context
                    and str(key).lower() in self._sensitive_value_keys
                    and item is not None
                    and item != ""
                    and item != ()
                    and item != []
                ):
                    output[key] = "[REDACTED]"
                    changed = True
                    continue
                sanitized, item_changed = self.redact_structure(item)
                output[key] = sanitized
                changed = changed or item_changed
            return output, changed
        return value, False
