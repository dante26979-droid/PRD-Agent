from __future__ import annotations

import os
import uuid

import pytest

from prd_agent.historical.ingest import HistoricalPrdIngestor
from prd_agent.historical.models import HistoricalPrdSearchArguments
from prd_agent.historical.retrieval import HistoricalPrdRetriever
from prd_agent.sources.models import (
    SourceBinding,
    SourceKind,
    access_scope_hash,
)
from prd_agent.storage.postgres_historical import PostgresHistoricalPrdStore


pytestmark = pytest.mark.skipif(
    not os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    reason="PRD_AGENT_TEST_DATABASE_DSN is required for PostgreSQL integration tests",
)


def test_postgres_ingest_retrieval_permission_filter_and_replay(tmp_path):
    suffix = uuid.uuid4().hex
    corpus_id = f"history-{suffix}"
    public_id = f"public-{suffix}"
    secret_id = f"secret-{suffix}"
    (tmp_path / "public.md").write_text(
        "# 订单规则\n\n历史状态为 completed。", encoding="utf-8"
    )
    (tmp_path / "secret.md").write_text(
        "# 机密订单规则\n\n机密状态为 completed。", encoding="utf-8"
    )
    (tmp_path / "manifest.yaml").write_text(
        f"""
corpus_id: {corpus_id}
owner_id: owner-integration
project_id: project-integration
documents:
  - document_id: {public_id}
    path: public.md
    source_uri: demo://{public_id}
    source_revision: "1"
    access_labels: []
  - document_id: {secret_id}
    path: secret.md
    source_uri: demo://{secret_id}
    source_revision: "1"
    access_labels: [secret]
""".strip(),
        encoding="utf-8",
    )
    store = PostgresHistoricalPrdStore.from_dsn(
        os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
    )
    try:
        corpus = HistoricalPrdIngestor(store).ingest_manifest(
            tmp_path / "manifest.yaml", allowed_root=tmp_path
        )
        repeated = HistoricalPrdIngestor(store).ingest_manifest(
            tmp_path / "manifest.yaml", allowed_root=tmp_path
        )
        binding = SourceBinding(
            binding_id=f"binding-{suffix}",
            source_kind=SourceKind.HISTORICAL_PRD_CORPUS,
            source_id=corpus_id,
            source_version=corpus.corpus_version,
            owner_id="owner-integration",
            access_scope_hash=access_scope_hash(
                "owner-integration", "project-integration", ()
            ),
            metadata={"project_id": "project-integration"},
        )
        result = HistoricalPrdRetriever(store).search(
            investigation_id=f"investigation-{suffix}",
            binding=binding,
            arguments=HistoricalPrdSearchArguments(query="订单 completed"),
            owner_id="owner-integration",
            project_id="project-integration",
        )
        restored_run, restored_hits = store.get_retrieval(
            result.run.retrieval_run_id
        )

        assert repeated.corpus_version == corpus.corpus_version
        assert [item.document_id for item in result.items] == [public_id]
        assert restored_run == result.run
        assert restored_hits == result.hits
    finally:
        with store.connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM retrieval_hits
                 WHERE retrieval_run_id IN (
                    SELECT retrieval_run_id FROM retrieval_runs
                     WHERE corpus_id = %s
                 )
                """,
                (corpus_id,),
            )
            cursor.execute(
                "DELETE FROM retrieval_runs WHERE corpus_id = %s", (corpus_id,)
            )
            cursor.execute(
                "DELETE FROM historical_corpus_documents WHERE corpus_id = %s",
                (corpus_id,),
            )
            cursor.execute(
                "DELETE FROM historical_corpus_chunks WHERE corpus_id = %s",
                (corpus_id,),
            )
            cursor.execute(
                "DELETE FROM historical_corpora WHERE corpus_id = %s", (corpus_id,)
            )
            cursor.execute(
                """
                DELETE FROM historical_prd_chunks
                 WHERE document_version_id IN (
                    SELECT document_version_id FROM historical_prd_versions
                     WHERE document_id = ANY(%s)
                 )
                """,
                ([public_id, secret_id],),
            )
            cursor.execute(
                "DELETE FROM historical_prd_versions WHERE document_id = ANY(%s)",
                ([public_id, secret_id],),
            )
            cursor.execute(
                "DELETE FROM historical_prd_documents WHERE document_id = ANY(%s)",
                ([public_id, secret_id],),
            )
        store.connection.commit()
        store.connection.close()
