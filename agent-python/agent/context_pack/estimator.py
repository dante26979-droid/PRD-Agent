from __future__ import annotations

from typing import Protocol

from agent.runtime.idempotency import canonical_json


class TokenEstimator(Protocol):
    def estimate_bytes(self, value: bytes) -> int: ...

    def estimate_request(self, system_prompt: str, payload: bytes) -> int: ...


class ConservativeTokenEstimator:
    """Provider-independent upper-biased estimator for UTF-8 JSON.

    It deliberately avoids treating the existing bytes/4 heuristic as a hard
    limit because that can underestimate CJK-heavy requests.
    """

    def __init__(self, *, bytes_per_token: float = 2.5, fixed_overhead: int = 16) -> None:
        if bytes_per_token <= 0:
            raise ValueError("bytes_per_token must be positive")
        self._bytes_per_token = bytes_per_token
        self._fixed_overhead = max(0, fixed_overhead)

    def estimate_bytes(self, value: bytes) -> int:
        return max(1, int(len(value) / self._bytes_per_token) + self._fixed_overhead)

    def estimate_request(self, system_prompt: str, payload: bytes) -> int:
        return self.estimate_bytes(system_prompt.encode("utf-8") + b"\x00" + payload)

    def estimate_value(self, value: object) -> int:
        return self.estimate_bytes(canonical_json(value))
