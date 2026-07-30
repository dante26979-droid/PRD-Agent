from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class InvestigationPlan:
    repository_queries: tuple[str, ...] = ()
    prd_query: str = ""

    @classmethod
    def from_model_output(
        cls,
        value: Mapping[str, object],
        *,
        max_repository_queries: int = 10,
    ) -> "InvestigationPlan":
        raw_queries = value.get("repository_queries", ())
        if not isinstance(raw_queries, (list, tuple)):
            raise ValueError("repository_queries must be an array")
        queries = []
        for item in raw_queries:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("repository query must be non-empty text")
            normalized = item.strip()
            if normalized not in queries:
                queries.append(normalized)
            if len(queries) >= max_repository_queries:
                break
        raw_prd_query = value.get("prd_query", "")
        if raw_prd_query is None:
            raw_prd_query = ""
        if not isinstance(raw_prd_query, str):
            raise ValueError("prd_query must be text")
        return cls(tuple(queries), raw_prd_query.strip())
