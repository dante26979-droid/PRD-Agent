from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import time
import uuid

from prd_agent.hashing import sha256_json
from prd_agent.sources.models import SourceBinding, SourceKind, access_scope_hash

from .models import (
    CorpusStatus,
    DocumentStatus,
    HistoricalPrdResultItem,
    HistoricalPrdSearchArguments,
    HistoricalRetrievalResult,
    RetrievalHit,
    RetrievalMode,
    RetrievalRun,
    RetrievalStatus,
)
from .text import safe_excerpt, tokenize


class HybridUnavailableError(RuntimeError):
    pass


def reciprocal_rank_fusion(
    keyword_chunk_ids: tuple[str, ...],
    vector_chunk_ids: tuple[str, ...],
    *,
    constant: int = 60,
) -> tuple[tuple[str, float, int | None, int | None], ...]:
    keyword_ranks = {
        chunk_id: rank for rank, chunk_id in enumerate(keyword_chunk_ids, start=1)
    }
    vector_ranks = {
        chunk_id: rank for rank, chunk_id in enumerate(vector_chunk_ids, start=1)
    }
    values = []
    for chunk_id in keyword_ranks.keys() | vector_ranks.keys():
        keyword_rank = keyword_ranks.get(chunk_id)
        vector_rank = vector_ranks.get(chunk_id)
        score = (
            (1 / (constant + keyword_rank) if keyword_rank else 0)
            + (1 / (constant + vector_rank) if vector_rank else 0)
        )
        values.append((chunk_id, score, keyword_rank, vector_rank))
    return tuple(sorted(values, key=lambda item: (-item[1], item[0])))


class HistoricalPrdRetriever:
    """Permission-first deterministic retrieval over a fixed corpus version."""

    def __init__(
        self,
        store,
        *,
        vector_ranker=None,
        stale_after_days: int = 365,
    ) -> None:
        self.store = store
        self.vector_ranker = vector_ranker
        self.stale_after_days = stale_after_days

    def search(
        self,
        *,
        investigation_id: str,
        binding: SourceBinding,
        arguments: HistoricalPrdSearchArguments,
        owner_id: str,
        project_id: str,
        access_labels: tuple[str, ...] = (),
        allow_keyword_fallback: bool = False,
        as_of: datetime | None = None,
    ) -> HistoricalRetrievalResult:
        started = time.perf_counter()
        run_id = f"retrieval-{uuid.uuid4().hex}"
        normalized_labels = tuple(sorted(set(access_labels)))
        self._validate_binding(
            binding,
            owner_id=owner_id,
            project_id=project_id,
            access_labels=normalized_labels,
        )
        corpus = self.store.get_corpus(binding.source_id, binding.source_version)
        if corpus.status != CorpusStatus.READY:
            return self._failure(
                run_id,
                investigation_id,
                binding,
                arguments,
                started,
                "历史语料尚未就绪",
            )
        if corpus.owner_id != owner_id or corpus.project_id != project_id:
            raise PermissionError("corpus is outside the authorized scope")

        labels = frozenset(normalized_labels)
        sql_ranker = getattr(self.store, "keyword_ranked_entries", None)
        if sql_ranker is not None:
            ranked_entries = sql_ranker(
                binding.source_id,
                binding.source_version,
                query=arguments.query,
                owner_id=owner_id,
                project_id=project_id,
                access_labels=labels,
                product_tags=frozenset(arguments.product_tags),
                updated_after=arguments.updated_after,
                limit=20,
            )
            entries = tuple(item[0] for item in ranked_entries)
            keyword_ranked = tuple(
                (entry[0].chunk_id, score) for entry, score in ranked_entries
            )
        else:
            entries = self._authorized_entries(
                binding,
                arguments,
                owner_id=owner_id,
                project_id=project_id,
                access_labels=labels,
            )
            keyword_ranked = self._keyword_rank(arguments.query, entries)[:20]
        fallback_mode = None
        if arguments.mode == RetrievalMode.KEYWORD:
            ranked = tuple(
                (chunk_id, None, rank, None)
                for rank, (chunk_id, _) in enumerate(keyword_ranked, start=1)
            )
        elif self.vector_ranker is None:
            if not allow_keyword_fallback:
                return self._failure(
                    run_id,
                    investigation_id,
                    binding,
                    arguments,
                    started,
                    "Hybrid 检索不可用",
                    error_code="HYBRID_UNAVAILABLE",
                )
            fallback_mode = RetrievalMode.KEYWORD
            ranked = tuple(
                (chunk_id, None, rank, None)
                for rank, (chunk_id, _) in enumerate(keyword_ranked, start=1)
            )
        else:
            if sql_ranker is not None:
                entries = self._authorized_entries(
                    binding,
                    arguments,
                    owner_id=owner_id,
                    project_id=project_id,
                    access_labels=labels,
                )
            candidate_ids = tuple(chunk.chunk_id for chunk, _, _ in entries)
            vector_ranked = tuple(
                self.vector_ranker.rank(arguments.query, entries)
            )[:20]
            if any(chunk_id not in candidate_ids for chunk_id in vector_ranked):
                raise ValueError("vector ranker returned an unauthorized chunk")
            ranked = reciprocal_rank_fusion(
                tuple(item[0] for item in keyword_ranked),
                vector_ranked,
            )

        entry_by_chunk = {
            chunk.chunk_id: (chunk, version, document)
            for chunk, version, document in entries
        }
        selected = tuple(item for item in ranked if item[0] in entry_by_chunk)[
            : arguments.limit
        ]
        now = as_of or datetime.now(timezone.utc)
        hits: list[RetrievalHit] = []
        items: list[HistoricalPrdResultItem] = []
        for final_rank, (chunk_id, fused_score, keyword_rank, vector_rank) in enumerate(
            selected, start=1
        ):
            chunk, version, document = entry_by_chunk[chunk_id]
            stale = bool(
                document.updated_at_source
                and document.updated_at_source < now - timedelta(days=self.stale_after_days)
            )
            excerpt, _ = safe_excerpt(chunk.content)
            hits.append(
                RetrievalHit(
                    retrieval_run_id=run_id,
                    chunk_id=chunk_id,
                    rank=final_rank,
                    keyword_rank=keyword_rank,
                    vector_rank=vector_rank,
                    fused_score=fused_score,
                    stale_hint=stale,
                )
            )
            items.append(
                HistoricalPrdResultItem(
                    document_id=document.document_id,
                    document_version_id=version.document_version_id,
                    chunk_id=chunk.chunk_id,
                    title=document.title,
                    section_path=chunk.section_path,
                    source_uri=document.source_uri,
                    updated_at_source=document.updated_at_source,
                    excerpt=excerpt,
                    content_hash=chunk.content_hash,
                    stale_hint=stale,
                )
            )
        status = RetrievalStatus.SUCCEEDED if hits else RetrievalStatus.EMPTY
        run = self._run(
            run_id,
            investigation_id,
            binding,
            arguments,
            started,
            status,
            fallback_mode=fallback_mode,
        )
        result = HistoricalRetrievalResult(
            run=run,
            hits=tuple(hits),
            items=tuple(items),
            public_summary=(
                f"历史 PRD 检索返回 {len(items)} 个章节片段"
                if items
                else "历史 PRD 检索未召回可用章节"
            ),
        )
        self.store.save_retrieval(run, tuple(hits))
        return result

    @staticmethod
    def _keyword_rank(query, entries):
        query_tokens = Counter(tokenize(query))
        if not query_tokens:
            return ()
        scored = []
        for chunk, _, document in entries:
            indexed = Counter(chunk.token_text.split())
            overlap = sum(
                min(count, indexed[token]) for token, count in query_tokens.items()
            )
            normalized_query = query.strip().lower()
            exact_bonus = 4 if normalized_query in document.title.lower() else 0
            tag_bonus = sum(
                2
                for tag in document.product_tags
                if tag.lower() in normalized_query or normalized_query in tag.lower()
            )
            score = overlap + exact_bonus + tag_bonus
            if score:
                updated = (
                    document.updated_at_source.timestamp()
                    if document.updated_at_source
                    else float("-inf")
                )
                scored.append((chunk.chunk_id, float(score), updated))
        return tuple(
            (chunk_id, score)
            for chunk_id, score, _ in sorted(
                scored, key=lambda item: (-item[1], -item[2], item[0])
            )
        )

    def _authorized_entries(
        self,
        binding,
        arguments,
        *,
        owner_id,
        project_id,
        access_labels,
    ):
        requested_tags = frozenset(arguments.product_tags)
        return self.store.authorized_entries(
            binding.source_id,
            binding.source_version,
            owner_id=owner_id,
            project_id=project_id,
            access_labels=access_labels,
            product_tags=requested_tags,
            updated_after=arguments.updated_after,
        )

    @staticmethod
    def _validate_binding(
        binding,
        *,
        owner_id,
        project_id,
        access_labels,
    ):
        if binding.source_kind != SourceKind.HISTORICAL_PRD_CORPUS:
            raise ValueError("historical retrieval requires a historical corpus binding")
        if binding.owner_id != owner_id:
            raise PermissionError("binding owner does not match the actor")
        expected = access_scope_hash(owner_id, project_id, access_labels)
        if binding.access_scope_hash != expected:
            raise PermissionError("binding access scope is stale or unauthorized")
        if binding.metadata.get("project_id") != project_id:
            raise PermissionError("binding project does not match the task")

    @staticmethod
    def _run(
        run_id,
        investigation_id,
        binding,
        arguments,
        started,
        status,
        *,
        fallback_mode=None,
    ):
        return RetrievalRun(
            retrieval_run_id=run_id,
            investigation_id=investigation_id,
            corpus_id=binding.source_id,
            corpus_version=binding.source_version,
            query_hash=sha256_json(arguments.query),
            mode=arguments.mode,
            filters_hash=sha256_json(
                {
                    "product_tags": arguments.product_tags,
                    "updated_after": arguments.updated_after,
                    "access_scope_hash": binding.access_scope_hash,
                }
            ),
            limit=arguments.limit,
            duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
            status=status,
            fallback_mode=fallback_mode,
        )

    def _failure(
        self,
        run_id,
        investigation_id,
        binding,
        arguments,
        started,
        summary,
        *,
        error_code=None,
    ):
        run = self._run(
            run_id,
            investigation_id,
            binding,
            arguments,
            started,
            RetrievalStatus.FAILED,
        )
        self.store.save_retrieval(run, ())
        return HistoricalRetrievalResult(
            run=run,
            public_summary=summary,
            error_code=error_code,
        )
