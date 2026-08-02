from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from time import perf_counter
from typing import Any, Mapping

from agent.capability import (
    PrdCatalogHit,
    PrdSection,
    RepositorySearchHit,
)


def _signature(name: str, arguments: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"capability": name, "arguments": dict(arguments)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CapabilityObservation:
    sequence: int
    capability_name: str
    action_signature: str
    status: str
    evidence_count: int
    duration_ms: int
    physical_call: bool = True
    durable_replay: bool = False
    error_category: str | None = None


class InstrumentedCapabilityGateway:
    """Static/delegating gateway that stores no query, path, locator or excerpt."""

    def __init__(
        self,
        *,
        repository_hits: Mapping[str, tuple[RepositorySearchHit, ...]] | None = None,
        catalog_hits: Mapping[str, tuple[PrdCatalogHit, ...]] | None = None,
        sections: Mapping[str, PrdSection] | None = None,
        delegate: object | None = None,
    ) -> None:
        self._repository_hits = dict(repository_hits or {})
        self._catalog_hits = dict(catalog_hits or {})
        self._sections = dict(sections or {})
        self._delegate = delegate
        self.observations: list[CapabilityObservation] = []
        self.closed = False

    @classmethod
    def from_fixture(cls, value: Mapping[str, Any]) -> "InstrumentedCapabilityGateway":
        repository_hits: dict[str, tuple[RepositorySearchHit, ...]] = {}
        for query, hits in value.get("repository_hits", {}).items():
            repository_hits[str(query)] = tuple(
                RepositorySearchHit(
                    path=str(item["path"]),
                    line=int(item["line"]),
                    snippet=str(item["snippet"]),
                )
                for item in hits
            )
        return cls(repository_hits=repository_hits)

    def search_repository(self, **kwargs):
        query = str(kwargs.get("query", ""))
        return self._observe(
            "search_repository",
            {"query": query, "limit": kwargs.get("limit")},
            lambda: (
                self._delegate.search_repository(**kwargs)
                if self._delegate is not None
                else self._repository_hits.get(query, ())
            ),
        )

    def search_prd_catalog(self, **kwargs):
        query = str(kwargs.get("query", ""))
        return self._observe(
            "search_prd_catalog",
            {"query": query, "limit": kwargs.get("limit")},
            lambda: (
                self._delegate.search_prd_catalog(**kwargs)
                if self._delegate is not None
                else self._catalog_hits.get(query, ())
            ),
        )

    def fetch_prd_sections(self, locator_ids):
        values = tuple(str(item) for item in locator_ids)
        return self._observe(
            "fetch_prd_sections",
            {"locator_ids": values},
            lambda: (
                self._delegate.fetch_prd_sections(values)
                if self._delegate is not None
                else tuple(self._sections[item] for item in values if item in self._sections)
            ),
        )

    def _observe(self, name: str, arguments: Mapping[str, Any], call):
        sequence = len(self.observations) + 1
        signature = _signature(name, arguments)
        started = perf_counter()
        try:
            result = tuple(call())
        except Exception as error:
            self.observations.append(
                CapabilityObservation(
                    sequence=sequence,
                    capability_name=name,
                    action_signature=signature,
                    status="FAILED",
                    evidence_count=0,
                    duration_ms=int((perf_counter() - started) * 1000),
                    error_category=getattr(error, "code", type(error).__name__),
                )
            )
            raise
        self.observations.append(
            CapabilityObservation(
                sequence=sequence,
                capability_name=name,
                action_signature=signature,
                status="SUCCEEDED" if result else "EMPTY",
                evidence_count=len(result),
                duration_ms=int((perf_counter() - started) * 1000),
            )
        )
        return result

    def close(self) -> None:
        self.closed = True
        close = getattr(self._delegate, "close", None)
        if callable(close):
            close()
