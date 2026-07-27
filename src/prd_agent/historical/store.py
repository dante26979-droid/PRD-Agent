from __future__ import annotations

from copy import deepcopy

from .models import (
    HistoricalCorpus,
    HistoricalPrdChunk,
    HistoricalPrdDocument,
    HistoricalPrdVersion,
    RetrievalHit,
    RetrievalRun,
)


class InMemoryHistoricalPrdStore:
    def __init__(self) -> None:
        self._corpora: dict[tuple[str, str], HistoricalCorpus] = {}
        self._corpus_chunks: dict[tuple[str, str], tuple[str, ...]] = {}
        self._corpus_documents: dict[
            tuple[str, str], dict[str, HistoricalPrdDocument]
        ] = {}
        self._documents: dict[
            tuple[str, str, str], HistoricalPrdDocument
        ] = {}
        self._versions: dict[str, HistoricalPrdVersion] = {}
        self._version_by_content: dict[tuple[str, str], str] = {}
        self._chunks: dict[str, HistoricalPrdChunk] = {}
        self._retrieval_runs: dict[str, RetrievalRun] = {}
        self._retrieval_hits: dict[str, tuple[RetrievalHit, ...]] = {}

    def get_version_by_content(
        self, document_id: str, content_hash: str
    ) -> HistoricalPrdVersion | None:
        version_id = self._version_by_content.get((document_id, content_hash))
        return deepcopy(self._versions.get(version_id)) if version_id else None

    def latest_version(self, document_id: str) -> HistoricalPrdVersion | None:
        versions = [
            value for value in self._versions.values() if value.document_id == document_id
        ]
        if not versions:
            return None
        return deepcopy(max(versions, key=lambda item: (item.imported_at, item.document_version_id)))

    def save_document(self, document: HistoricalPrdDocument) -> None:
        key = (document.owner_id, document.project_id, document.document_id)
        self._documents[key] = deepcopy(document)

    def save_version(self, version: HistoricalPrdVersion) -> HistoricalPrdVersion:
        existing = self.get_version_by_content(
            version.document_id, version.content_hash
        )
        if existing:
            return existing
        self._versions[version.document_version_id] = deepcopy(version)
        self._version_by_content[(version.document_id, version.content_hash)] = (
            version.document_version_id
        )
        return deepcopy(version)

    def save_chunks(self, chunks: tuple[HistoricalPrdChunk, ...]) -> None:
        for chunk in chunks:
            self._chunks.setdefault(chunk.chunk_id, deepcopy(chunk))

    def chunks_for_version(
        self, document_version_id: str
    ) -> tuple[HistoricalPrdChunk, ...]:
        return tuple(
            deepcopy(item)
            for item in sorted(
                self._chunks.values(), key=lambda candidate: candidate.ordinal
            )
            if item.document_version_id == document_version_id
        )

    def save_corpus(
        self,
        corpus: HistoricalCorpus,
        *,
        chunk_ids: tuple[str, ...],
        documents: tuple[
            tuple[str, HistoricalPrdDocument], ...
        ],
    ) -> HistoricalCorpus:
        key = (corpus.corpus_id, corpus.corpus_version)
        self._corpora.setdefault(key, deepcopy(corpus))
        self._corpus_chunks.setdefault(key, tuple(chunk_ids))
        self._corpus_documents.setdefault(
            key,
            {
                version_id: deepcopy(document)
                for version_id, document in documents
            },
        )
        return deepcopy(self._corpora[key])

    def get_corpus(self, corpus_id: str, corpus_version: str) -> HistoricalCorpus:
        return deepcopy(self._corpora[(corpus_id, corpus_version)])

    def entries(
        self, corpus_id: str, corpus_version: str
    ) -> tuple[
        tuple[HistoricalPrdChunk, HistoricalPrdVersion, HistoricalPrdDocument], ...
    ]:
        key = (corpus_id, corpus_version)
        result = []
        for chunk_id in self._corpus_chunks[key]:
            chunk = self._chunks[chunk_id]
            version = self._versions[chunk.document_version_id]
            document = self._corpus_documents[key][version.document_version_id]
            result.append((deepcopy(chunk), deepcopy(version), deepcopy(document)))
        return tuple(result)

    def authorized_entries(
        self,
        corpus_id: str,
        corpus_version: str,
        *,
        owner_id: str,
        project_id: str,
        access_labels: frozenset[str],
        product_tags: frozenset[str],
        updated_after,
    ):
        result = []
        for entry in self.entries(corpus_id, corpus_version):
            _, _, document = entry
            if document.owner_id != owner_id or document.project_id != project_id:
                continue
            if document.status.value != "ACTIVE":
                continue
            if not frozenset(document.access_labels).issubset(access_labels):
                continue
            if product_tags and not product_tags.intersection(document.product_tags):
                continue
            if (
                updated_after
                and (
                    document.updated_at_source is None
                    or document.updated_at_source < updated_after
                )
            ):
                continue
            result.append(entry)
        return tuple(result)

    def save_retrieval(
        self, run: RetrievalRun, hits: tuple[RetrievalHit, ...]
    ) -> None:
        self._retrieval_runs[run.retrieval_run_id] = deepcopy(run)
        self._retrieval_hits[run.retrieval_run_id] = deepcopy(hits)

    def get_retrieval(
        self, retrieval_run_id: str
    ) -> tuple[RetrievalRun, tuple[RetrievalHit, ...]]:
        return (
            deepcopy(self._retrieval_runs[retrieval_run_id]),
            deepcopy(self._retrieval_hits[retrieval_run_id]),
        )
