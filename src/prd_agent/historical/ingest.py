from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from prd_agent.hashing import canonical_json

from .chunking import chunk_markdown, content_hash
from .manifest import HistoricalCorpusManifest, load_manifest
from .models import (
    CorpusStatus,
    HistoricalCorpus,
    HistoricalPrdDocument,
    HistoricalPrdVersion,
)
from .text import TOKENIZER_VERSION, normalize_markdown


def _identifier(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


class HistoricalPrdIngestor:
    def __init__(self, store, *, max_document_bytes: int = 2_000_000) -> None:
        self.store = store
        self.max_document_bytes = max_document_bytes

    def ingest_manifest(
        self,
        manifest_path: str | Path,
        *,
        allowed_root: str | Path,
        embedding_model_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> HistoricalCorpus:
        manifest, document_root = load_manifest(
            manifest_path, allowed_root=allowed_root
        )
        return self.ingest(
            manifest,
            document_root=document_root,
            embedding_model_id=embedding_model_id,
            imported_at=imported_at,
        )

    def ingest(
        self,
        manifest: HistoricalCorpusManifest,
        *,
        document_root: str | Path,
        embedding_model_id: str | None = None,
        imported_at: datetime | None = None,
    ) -> HistoricalCorpus:
        root = Path(document_root).resolve(strict=True)
        timestamp = imported_at or datetime.now(timezone.utc)
        versions: list[HistoricalPrdVersion] = []
        document_snapshots: list[tuple[str, HistoricalPrdDocument]] = []
        chunk_ids: list[str] = []
        for manifest_document in manifest.documents:
            path = (root / manifest_document.path).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("document path escapes the manifest directory")
            data = path.read_bytes()
            if len(data) > self.max_document_bytes:
                raise ValueError(f"document exceeds size limit: {manifest_document.path}")
            if b"\x00" in data:
                raise ValueError(f"binary document is not allowed: {manifest_document.path}")
            try:
                raw_markdown = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"document must be UTF-8: {manifest_document.path}"
                ) from exc
            markdown = normalize_markdown(raw_markdown)
            hashed = content_hash(markdown)
            document = HistoricalPrdDocument(
                document_id=manifest_document.document_id,
                owner_id=manifest.owner_id,
                project_id=manifest.project_id,
                title=manifest_document.title
                or _first_heading(markdown)
                or manifest_document.document_id,
                source_uri=manifest_document.source_uri,
                access_labels=tuple(sorted(set(manifest_document.access_labels))),
                product_tags=tuple(sorted(set(manifest_document.product_tags))),
                created_at_source=manifest_document.created_at_source,
                updated_at_source=manifest_document.updated_at_source,
            )
            self.store.save_document(document)
            existing = self.store.get_version_by_content(document.document_id, hashed)
            if existing:
                version = existing
                chunks = self.store.chunks_for_version(existing.document_version_id)
                if not chunks:
                    chunks = chunk_markdown(
                        existing.markdown,
                        document_version_id=existing.document_version_id,
                        title=document.title,
                        product_tags=document.product_tags,
                    )
                    self.store.save_chunks(chunks)
            else:
                previous = self.store.latest_version(document.document_id)
                version = HistoricalPrdVersion(
                    document_version_id=_identifier(
                        "docver",
                        {
                            "document_id": document.document_id,
                            "source_revision": manifest_document.source_revision,
                            "content_hash": hashed,
                        },
                    ),
                    document_id=document.document_id,
                    source_revision=manifest_document.source_revision,
                    content_hash=hashed,
                    markdown=markdown,
                    imported_at=timestamp,
                    supersedes_version_id=(
                        previous.document_version_id if previous else None
                    ),
                )
                version = self.store.save_version(version)
                chunks = chunk_markdown(
                    markdown,
                    document_version_id=version.document_version_id,
                    title=document.title,
                    product_tags=document.product_tags,
                )
                self.store.save_chunks(chunks)
            versions.append(version)
            document_snapshots.append((version.document_version_id, document))
            chunk_ids.extend(chunk.chunk_id for chunk in chunks)

        corpus_version = _identifier(
            "corpus",
            {
                "versions": sorted(
                    (item.document_version_id, item.content_hash) for item in versions
                ),
                "tokenizer_version": TOKENIZER_VERSION,
                "embedding_model_id": embedding_model_id,
                "manifest_hash": manifest.manifest_hash,
            },
        )
        corpus = HistoricalCorpus(
            corpus_id=manifest.corpus_id,
            owner_id=manifest.owner_id,
            project_id=manifest.project_id,
            corpus_version=corpus_version,
            manifest_hash=manifest.manifest_hash,
            document_count=len(versions),
            chunk_count=len(chunk_ids),
            tokenizer_version=TOKENIZER_VERSION,
            embedding_model_id=embedding_model_id,
            status=CorpusStatus.READY,
            created_at=timestamp,
        )
        return self.store.save_corpus(
            corpus,
            chunk_ids=tuple(chunk_ids),
            documents=tuple(document_snapshots),
        )

def _first_heading(markdown: str) -> str | None:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None
