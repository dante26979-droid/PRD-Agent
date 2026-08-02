from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from agent.checkpoint import CheckpointError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, order=True)
class HashDigest:
    value: str

    @classmethod
    def parse(cls, value: str) -> "HashDigest":
        raw = value.removeprefix("sha256:")
        if not _SHA256.fullmatch(raw):
            raise CheckpointError("invalid SHA-256 digest")
        return cls("sha256:" + raw)

    @classmethod
    def of_bytes(cls, value: bytes) -> "HashDigest":
        return cls("sha256:" + hashlib.sha256(value).hexdigest())

    @classmethod
    def of_text(cls, value: str) -> "HashDigest":
        return cls.of_bytes(value.encode("utf-8"))

    def __str__(self) -> str:
        return self.value
