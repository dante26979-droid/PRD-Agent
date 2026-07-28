from datetime import datetime, timezone

import pytest

from prd_agent.historical.ingest import HistoricalPrdIngestor
from prd_agent.historical.manifest import load_manifest
from prd_agent.historical.models import CorpusStatus
from prd_agent.historical.store import InMemoryHistoricalPrdStore

from .support import write_fixture


def test_manifest_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside-prd.md"
    outside.write_text("# secret", encoding="utf-8")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        f"""
corpus_id: demo
owner_id: owner
project_id: project
documents:
  - document_id: escaped
    path: ../{outside.name}
    source_uri: demo://escaped
    source_revision: "1"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="relative|escape"):
        load_manifest(manifest, allowed_root=tmp_path)


def test_manifest_rejects_uncontrolled_source_scheme(tmp_path):
    (tmp_path / "doc.md").write_text("# Doc", encoding="utf-8")
    (tmp_path / "manifest.yaml").write_text(
        """
corpus_id: demo
owner_id: owner
project_id: project
documents:
  - document_id: doc
    path: doc.md
    source_uri: file:///tmp/doc.md
    source_revision: "1"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="https or demo"):
        load_manifest(tmp_path / "manifest.yaml", allowed_root=tmp_path)


def test_ingest_is_idempotent_and_corpus_version_is_deterministic(tmp_path):
    store, first = write_fixture(tmp_path)

    second = HistoricalPrdIngestor(store).ingest_manifest(
        tmp_path / "manifest.yaml",
        allowed_root=tmp_path,
        imported_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    assert first.status == CorpusStatus.READY
    assert second.corpus_version == first.corpus_version
    assert second.document_count == 2
    assert second.chunk_count == first.chunk_count
    assert len(store.entries(first.corpus_id, first.corpus_version)) == first.chunk_count


def test_ranking_metadata_change_creates_new_corpus_and_preserves_old_snapshot(
    tmp_path,
):
    store, first = write_fixture(tmp_path)
    manifest_path = tmp_path / "manifest.yaml"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        original.replace(
            "document_id: public-order\n    path:",
            "document_id: public-order\n    title: 新订单标题\n    path:",
        ),
        encoding="utf-8",
    )

    second = HistoricalPrdIngestor(store).ingest_manifest(
        manifest_path,
        allowed_root=tmp_path,
    )

    first_documents = {
        document.document_id: document
        for _, _, document in store.entries(first.corpus_id, first.corpus_version)
    }
    second_documents = {
        document.document_id: document
        for _, _, document in store.entries(second.corpus_id, second.corpus_version)
    }
    assert second.corpus_version != first.corpus_version
    assert first_documents["public-order"].title == "订单状态改造"
    assert second_documents["public-order"].title == "新订单标题"
