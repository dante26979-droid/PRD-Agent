from datetime import datetime, timezone
from pathlib import Path

from prd_agent.historical.ingest import HistoricalPrdIngestor
from prd_agent.historical.store import InMemoryHistoricalPrdStore
from prd_agent.sources.models import (
    SourceBinding,
    SourceKind,
    access_scope_hash,
)


def write_fixture(root: Path) -> tuple[InMemoryHistoricalPrdStore, object]:
    (root / "public.md").write_text(
        "# 订单状态改造\n\n## 业务规则\n\n历史方案使用 completed 作为完成态。\n",
        encoding="utf-8",
    )
    (root / "restricted.md").write_text(
        "# 机密订单状态\n\n## 业务规则\n\n机密方案也使用 completed，但不得公开。\n",
        encoding="utf-8",
    )
    (root / "manifest.yaml").write_text(
        """
corpus_id: demo-prds
owner_id: owner-1
project_id: project-1
documents:
  - document_id: public-order
    path: public.md
    source_uri: demo://historical-prds/public-order
    source_revision: "2025-01"
    access_labels: []
    product_tags: [order, status]
    updated_at_source: "2025-01-01T00:00:00Z"
  - document_id: restricted-order
    path: restricted.md
    source_uri: https://example.test/restricted-order
    source_revision: "2025-02"
    access_labels: [secret]
    product_tags: [order, status]
    updated_at_source: "2025-02-01T00:00:00Z"
""".strip(),
        encoding="utf-8",
    )
    store = InMemoryHistoricalPrdStore()
    corpus = HistoricalPrdIngestor(store).ingest_manifest(
        root / "manifest.yaml",
        allowed_root=root,
        imported_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    return store, corpus


def binding_for(corpus, labels=()):
    return SourceBinding(
        binding_id="binding-history",
        source_kind=SourceKind.HISTORICAL_PRD_CORPUS,
        source_id=corpus.corpus_id,
        source_version=corpus.corpus_version,
        owner_id=corpus.owner_id,
        access_scope_hash=access_scope_hash(
            corpus.owner_id, corpus.project_id, labels
        ),
        metadata={"project_id": corpus.project_id},
    )
