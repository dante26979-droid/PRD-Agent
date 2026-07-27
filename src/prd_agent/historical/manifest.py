from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prd_agent.hashing import sha256_json


class ManifestDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str = Field(min_length=1)
    title: str | None = None
    path: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    access_labels: tuple[str, ...] = ()
    product_tags: tuple[str, ...] = ()
    created_at_source: datetime | None = None
    updated_at_source: datetime | None = None

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str) -> str:
        if urlparse(value).scheme not in {"https", "demo"}:
            raise ValueError("source_uri must use https or demo")
        return value

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("document path must be relative and cannot escape its root")
        return path.as_posix()


class HistoricalCorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    corpus_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    documents: tuple[ManifestDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def document_ids_are_unique(self) -> "HistoricalCorpusManifest":
        ids = [item.document_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("document_id values must be unique")
        return self

    @property
    def manifest_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


def load_manifest(
    manifest_path: str | Path,
    *,
    allowed_root: str | Path,
) -> tuple[HistoricalCorpusManifest, Path]:
    root = Path(allowed_root).resolve(strict=True)
    path = Path(manifest_path).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError("manifest path escapes the configured root")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest = HistoricalCorpusManifest.model_validate(payload)
    document_root = path.parent.resolve(strict=True)
    for item in manifest.documents:
        resolved = (document_root / item.path).resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValueError(f"document path escapes the configured root: {item.path}")
        if not resolved.is_file():
            raise ValueError(f"document path is not a file: {item.path}")
    return manifest, document_root
