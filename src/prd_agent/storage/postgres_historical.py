from __future__ import annotations

from prd_agent.historical.models import (
    HistoricalCorpus,
    HistoricalPrdChunk,
    HistoricalPrdDocument,
    HistoricalPrdVersion,
    RetrievalHit,
    RetrievalRun,
)
from prd_agent.historical.text import tokenize


class PostgresHistoricalPrdStore:
    """PostgreSQL store whose retrieval query applies authorization before ranking."""

    def __init__(self, connection) -> None:
        self.connection = connection

    @classmethod
    def from_dsn(cls, dsn: str) -> "PostgresHistoricalPrdStore":
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError(
                "PostgreSQL historical PRD support requires: "
                "python -m pip install '.[postgres]'"
            ) from exc
        return cls(psycopg.connect(dsn))

    def get_version_by_content(self, document_id: str, content_hash: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_version_id, document_id, source_revision,
                       content_hash, markdown, imported_at, supersedes_version_id
                  FROM historical_prd_versions
                 WHERE document_id = %s AND content_hash = %s
                """,
                (document_id, content_hash),
            )
            row = cursor.fetchone()
        return self._version(row) if row else None

    def latest_version(self, document_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT document_version_id, document_id, source_revision,
                       content_hash, markdown, imported_at, supersedes_version_id
                  FROM historical_prd_versions
                 WHERE document_id = %s
                 ORDER BY imported_at DESC, document_version_id DESC
                 LIMIT 1
                """,
                (document_id,),
            )
            row = cursor.fetchone()
        return self._version(row) if row else None

    def save_document(self, document: HistoricalPrdDocument) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO historical_prd_documents (
                    document_id, owner_id, project_id, title, source_uri,
                    access_labels, product_tags, created_at_source,
                    updated_at_source, status
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    source_uri = EXCLUDED.source_uri,
                    access_labels = EXCLUDED.access_labels,
                    product_tags = EXCLUDED.product_tags,
                    created_at_source = EXCLUDED.created_at_source,
                    updated_at_source = EXCLUDED.updated_at_source,
                    status = EXCLUDED.status
                WHERE historical_prd_documents.owner_id = EXCLUDED.owner_id
                  AND historical_prd_documents.project_id = EXCLUDED.project_id
                """,
                (
                    document.document_id,
                    document.owner_id,
                    document.project_id,
                    document.title,
                    document.source_uri,
                    list(document.access_labels),
                    list(document.product_tags),
                    document.created_at_source,
                    document.updated_at_source,
                    document.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("document_id belongs to another owner or project")
        self.connection.commit()

    def save_version(self, version: HistoricalPrdVersion) -> HistoricalPrdVersion:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO historical_prd_versions (
                    document_version_id, document_id, source_revision,
                    content_hash, markdown, imported_at, supersedes_version_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (document_id, content_hash) DO NOTHING
                """,
                (
                    version.document_version_id,
                    version.document_id,
                    version.source_revision,
                    version.content_hash,
                    version.markdown,
                    version.imported_at,
                    version.supersedes_version_id,
                ),
            )
        self.connection.commit()
        return self.get_version_by_content(version.document_id, version.content_hash)

    def save_chunks(self, chunks: tuple[HistoricalPrdChunk, ...]) -> None:
        with self.connection.cursor() as cursor:
            for chunk in chunks:
                cursor.execute(
                    """
                    INSERT INTO historical_prd_chunks (
                        chunk_id, document_version_id, section_path, ordinal,
                        content, content_hash, token_text, token_count
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chunk_id) DO NOTHING
                    """,
                    (
                        chunk.chunk_id,
                        chunk.document_version_id,
                        list(chunk.section_path),
                        chunk.ordinal,
                        chunk.content,
                        chunk.content_hash,
                        chunk.token_text,
                        chunk.token_count,
                    ),
                )
        self.connection.commit()

    def chunks_for_version(self, document_version_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT chunk_id, document_version_id, section_path, ordinal,
                       content, content_hash, token_text, token_count
                  FROM historical_prd_chunks
                 WHERE document_version_id = %s
                 ORDER BY ordinal, chunk_id
                """,
                (document_version_id,),
            )
            rows = cursor.fetchall()
        return tuple(self._chunk(row) for row in rows)

    def save_corpus(
        self,
        corpus: HistoricalCorpus,
        *,
        chunk_ids: tuple[str, ...],
        documents: tuple[
            tuple[str, HistoricalPrdDocument], ...
        ],
    ) -> HistoricalCorpus:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO historical_corpora (
                    corpus_id, owner_id, project_id, corpus_version,
                    manifest_hash, document_count, chunk_count,
                    tokenizer_version, embedding_model_id, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (corpus_id, corpus_version) DO NOTHING
                """,
                (
                    corpus.corpus_id,
                    corpus.owner_id,
                    corpus.project_id,
                    corpus.corpus_version,
                    corpus.manifest_hash,
                    corpus.document_count,
                    corpus.chunk_count,
                    corpus.tokenizer_version,
                    corpus.embedding_model_id,
                    corpus.status.value,
                    corpus.created_at,
                ),
            )
            for chunk_id in chunk_ids:
                cursor.execute(
                    """
                    INSERT INTO historical_corpus_chunks (
                        corpus_id, corpus_version, chunk_id
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (corpus.corpus_id, corpus.corpus_version, chunk_id),
                )
            for document_version_id, document in documents:
                cursor.execute(
                    """
                    INSERT INTO historical_corpus_documents (
                        corpus_id, corpus_version, document_version_id,
                        document_id, owner_id, project_id, title, source_uri,
                        access_labels, product_tags, created_at_source,
                        updated_at_source, status
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        corpus.corpus_id,
                        corpus.corpus_version,
                        document_version_id,
                        document.document_id,
                        document.owner_id,
                        document.project_id,
                        document.title,
                        document.source_uri,
                        list(document.access_labels),
                        list(document.product_tags),
                        document.created_at_source,
                        document.updated_at_source,
                        document.status.value,
                    ),
                )
        self.connection.commit()
        return self.get_corpus(corpus.corpus_id, corpus.corpus_version)

    def get_corpus(self, corpus_id: str, corpus_version: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT corpus_id, owner_id, project_id, corpus_version,
                       manifest_hash, document_count, chunk_count,
                       tokenizer_version, embedding_model_id, status, created_at
                  FROM historical_corpora
                 WHERE corpus_id = %s AND corpus_version = %s
                """,
                (corpus_id, corpus_version),
            )
            row = cursor.fetchone()
        if not row:
            raise KeyError((corpus_id, corpus_version))
        return HistoricalCorpus(
            corpus_id=row[0],
            owner_id=row[1],
            project_id=row[2],
            corpus_version=row[3],
            manifest_hash=row[4],
            document_count=row[5],
            chunk_count=row[6],
            tokenizer_version=row[7],
            embedding_model_id=row[8],
            status=row[9],
            created_at=row[10],
        )

    def entries(self, corpus_id: str, corpus_version: str):
        return self._entries_query(
            corpus_id,
            corpus_version,
            where_sql="",
            parameters=(),
        )

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
        clauses = [
            "d.owner_id = %s",
            "d.project_id = %s",
            "d.status = 'ACTIVE'",
            "d.access_labels <@ %s::text[]",
        ]
        parameters: list[object] = [
            owner_id,
            project_id,
            list(sorted(access_labels)),
        ]
        if product_tags:
            clauses.append("d.product_tags && %s::text[]")
            parameters.append(list(sorted(product_tags)))
        if updated_after:
            clauses.append("d.updated_at_source >= %s")
            parameters.append(updated_after)
        return self._entries_query(
            corpus_id,
            corpus_version,
            where_sql=" AND " + " AND ".join(clauses),
            parameters=tuple(parameters),
        )

    def keyword_ranked_entries(
        self,
        corpus_id: str,
        corpus_version: str,
        *,
        query: str,
        owner_id: str,
        project_id: str,
        access_labels: frozenset[str],
        product_tags: frozenset[str],
        updated_after,
        limit: int,
    ):
        query_text = " ".join(tokenize(query))
        if not query_text:
            return ()
        clauses = [
            "cc.corpus_id = %s",
            "cc.corpus_version = %s",
            "d.owner_id = %s",
            "d.project_id = %s",
            "d.status = 'ACTIVE'",
            "d.access_labels <@ %s::text[]",
            (
                "to_tsvector('simple', c.token_text) "
                "@@ plainto_tsquery('simple', %s)"
            ),
        ]
        where_parameters: list[object] = [
            corpus_id,
            corpus_version,
            owner_id,
            project_id,
            list(sorted(access_labels)),
            query_text,
        ]
        if product_tags:
            clauses.append("d.product_tags && %s::text[]")
            where_parameters.append(list(sorted(product_tags)))
        if updated_after:
            clauses.append("d.updated_at_source >= %s")
            where_parameters.append(updated_after)
        parameters = [
            query_text,
            query.lower(),
            query.lower(),
            *where_parameters,
            limit,
        ]
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.chunk_id, c.document_version_id, c.section_path,
                    c.ordinal, c.content, c.content_hash, c.token_text,
                    c.token_count,
                    v.document_version_id, v.document_id, v.source_revision,
                    v.content_hash, v.markdown, v.imported_at,
                    v.supersedes_version_id,
                    d.document_id, d.owner_id, d.project_id, d.title,
                    d.source_uri, d.access_labels, d.product_tags,
                    d.created_at_source, d.updated_at_source, d.status,
                    (
                        ts_rank_cd(
                            to_tsvector('simple', c.token_text),
                            plainto_tsquery('simple', %s)
                        )
                        + CASE WHEN lower(d.title) = %s THEN 4 ELSE 0 END
                        + CASE WHEN %s = ANY (
                            SELECT lower(tag) FROM unnest(d.product_tags) AS tag
                          ) THEN 2 ELSE 0 END
                    ) AS score
                FROM historical_corpus_chunks cc
                JOIN historical_prd_chunks c ON c.chunk_id = cc.chunk_id
                JOIN historical_prd_versions v
                  ON v.document_version_id = c.document_version_id
                JOIN historical_corpus_documents d
                  ON d.corpus_id = cc.corpus_id
                 AND d.corpus_version = cc.corpus_version
                 AND d.document_version_id = v.document_version_id
                WHERE """
                + " AND ".join(clauses)
                + """
                ORDER BY score DESC, d.updated_at_source DESC NULLS LAST,
                         c.chunk_id ASC
                LIMIT %s
                """,
                tuple(parameters),
            )
            rows = cursor.fetchall()
        return tuple((self._entry(row[:25]), float(row[25])) for row in rows)

    def _entries_query(
        self, corpus_id, corpus_version, *, where_sql, parameters
    ):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    c.chunk_id, c.document_version_id, c.section_path,
                    c.ordinal, c.content, c.content_hash, c.token_text,
                    c.token_count,
                    v.document_version_id, v.document_id, v.source_revision,
                    v.content_hash, v.markdown, v.imported_at,
                    v.supersedes_version_id,
                    d.document_id, d.owner_id, d.project_id, d.title,
                    d.source_uri, d.access_labels, d.product_tags,
                    d.created_at_source, d.updated_at_source, d.status
                FROM historical_corpus_chunks cc
                JOIN historical_prd_chunks c ON c.chunk_id = cc.chunk_id
                JOIN historical_prd_versions v
                  ON v.document_version_id = c.document_version_id
                JOIN historical_corpus_documents d
                  ON d.corpus_id = cc.corpus_id
                 AND d.corpus_version = cc.corpus_version
                 AND d.document_version_id = v.document_version_id
                WHERE cc.corpus_id = %s AND cc.corpus_version = %s
                """
                + where_sql
                + " ORDER BY c.chunk_id",
                (corpus_id, corpus_version, *parameters),
            )
            rows = cursor.fetchall()
        return tuple(self._entry(row) for row in rows)

    def save_retrieval(
        self, run: RetrievalRun, hits: tuple[RetrievalHit, ...]
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO retrieval_runs (
                    retrieval_run_id, investigation_id, corpus_id,
                    corpus_version, query_hash, mode, filters_hash,
                    result_limit, duration_ms, status, fallback_mode, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (retrieval_run_id) DO NOTHING
                """,
                (
                    run.retrieval_run_id,
                    run.investigation_id,
                    run.corpus_id,
                    run.corpus_version,
                    run.query_hash,
                    run.mode.value,
                    run.filters_hash,
                    run.limit,
                    run.duration_ms,
                    run.status.value,
                    run.fallback_mode.value if run.fallback_mode else None,
                    run.created_at,
                ),
            )
            for hit in hits:
                cursor.execute(
                    """
                    INSERT INTO retrieval_hits (
                        retrieval_run_id, chunk_id, rank, keyword_rank,
                        vector_rank, fused_score, stale_hint
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (retrieval_run_id, rank) DO NOTHING
                    """,
                    (
                        hit.retrieval_run_id,
                        hit.chunk_id,
                        hit.rank,
                        hit.keyword_rank,
                        hit.vector_rank,
                        hit.fused_score,
                        hit.stale_hint,
                    ),
                )
        self.connection.commit()

    def get_retrieval(self, retrieval_run_id: str):
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT retrieval_run_id, investigation_id, corpus_id,
                       corpus_version, query_hash, mode, filters_hash,
                       result_limit, duration_ms, status, fallback_mode, created_at
                  FROM retrieval_runs WHERE retrieval_run_id = %s
                """,
                (retrieval_run_id,),
            )
            row = cursor.fetchone()
            if not row:
                raise KeyError(retrieval_run_id)
            cursor.execute(
                """
                SELECT retrieval_run_id, chunk_id, rank, keyword_rank,
                       vector_rank, fused_score, stale_hint
                  FROM retrieval_hits
                 WHERE retrieval_run_id = %s ORDER BY rank
                """,
                (retrieval_run_id,),
            )
            hit_rows = cursor.fetchall()
        run = RetrievalRun(
            retrieval_run_id=row[0],
            investigation_id=row[1],
            corpus_id=row[2],
            corpus_version=row[3],
            query_hash=row[4],
            mode=row[5],
            filters_hash=row[6],
            limit=row[7],
            duration_ms=row[8],
            status=row[9],
            fallback_mode=row[10],
            created_at=row[11],
        )
        return run, tuple(
            RetrievalHit(
                retrieval_run_id=item[0],
                chunk_id=item[1],
                rank=item[2],
                keyword_rank=item[3],
                vector_rank=item[4],
                fused_score=item[5],
                stale_hint=item[6],
            )
            for item in hit_rows
        )

    @staticmethod
    def _version(row):
        return HistoricalPrdVersion(
            document_version_id=row[0],
            document_id=row[1],
            source_revision=row[2],
            content_hash=row[3],
            markdown=row[4],
            imported_at=row[5],
            supersedes_version_id=row[6],
        )

    @staticmethod
    def _chunk(row):
        return HistoricalPrdChunk(
            chunk_id=row[0],
            document_version_id=row[1],
            section_path=tuple(row[2]),
            ordinal=row[3],
            content=row[4],
            content_hash=row[5],
            token_text=row[6],
            token_count=row[7],
        )

    @classmethod
    def _entry(cls, row):
        return (
            cls._chunk(row[:8]),
            cls._version(row[8:15]),
            HistoricalPrdDocument(
                document_id=row[15],
                owner_id=row[16],
                project_id=row[17],
                title=row[18],
                source_uri=row[19],
                access_labels=tuple(row[20]),
                product_tags=tuple(row[21]),
                created_at_source=row[22],
                updated_at_source=row[23],
                status=row[24],
            ),
        )
