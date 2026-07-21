"""Load and validate versioned evaluation cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EvalCase, EvalDataset


class DatasetValidationError(ValueError):
    """Raised when a dataset manifest or case violates the public contract."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetValidationError(f"cannot read JSON dataset file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DatasetValidationError(f"dataset file must contain an object: {path}")
    return value


def load_dataset(manifest_path: str | Path) -> EvalDataset:
    manifest_file = Path(manifest_path).resolve()
    manifest = _read_json(manifest_file)
    dataset_version = str(manifest.get("dataset_version", "")).strip()
    repository_id = str(manifest.get("repository_id", "")).strip()
    commit = str(manifest.get("resolved_commit_sha", "")).strip()
    case_paths = manifest.get("cases")
    if not dataset_version or not repository_id or not isinstance(case_paths, list):
        raise DatasetValidationError("manifest requires dataset_version, repository_id and cases")
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit.lower()):
        raise DatasetValidationError("manifest resolved_commit_sha must be a 40-character SHA")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for relative_path in case_paths:
        case_file = (manifest_file.parent / str(relative_path)).resolve()
        try:
            case = EvalCase.from_dict(_read_json(case_file))
        except (OSError, ValueError) as exc:
            if isinstance(exc, DatasetValidationError):
                raise
            raise DatasetValidationError(f"invalid case {case_file}: {exc}") from exc
        if case.case_id in seen:
            raise DatasetValidationError(f"duplicate case_id: {case.case_id}")
        if case.repository_id != repository_id or case.resolved_commit_sha != commit:
            raise DatasetValidationError(
                f"case {case.case_id} does not match manifest repository revision"
            )
        seen.add(case.case_id)
        cases.append(case)

    if not cases:
        raise DatasetValidationError("dataset must contain at least one case")
    return EvalDataset(
        dataset_version=dataset_version,
        repository_id=repository_id,
        resolved_commit_sha=commit,
        cases=tuple(cases),
    )
