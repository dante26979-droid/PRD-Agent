import subprocess
import unittest
from unittest.mock import patch

from prd_agent.evidence.deterministic_validator import (
    DeterministicEvidenceValidator,
    EvidenceValidationError,
)
from prd_agent.evidence.models import ExtractionMethod, SourceEvidence
from prd_agent.repository.bindings import (
    RepositoryBinding,
    RepositoryCatalog,
    normalize_relative_path,
)
from prd_agent.repository.errors import BlockedPath, InvalidRevision, RepositoryError
from prd_agent.repository.git_cli_reader import GitCliObjectReader

from tests.repository.support import TemporaryGitRepository


class RepositorySecurityBoundaryTests(unittest.TestCase):
    def test_rejects_non_canonical_relative_paths(self) -> None:
        for value in ("src//rules.py", "src/./rules.py"):
            with self.subTest(value=value):
                with self.assertRaises(BlockedPath):
                    normalize_relative_path(value)

    def test_empty_explicit_revision_is_not_defaulted(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "RULE = 1\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )

        with self.assertRaises(InvalidRevision):
            reader.resolve_snapshot("demo", "")

    def test_git_timeout_is_exposed_as_repository_error(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )

        with patch(
            "prd_agent.repository.git_cli_reader.subprocess.run",
            side_effect=subprocess.TimeoutExpired("git", 0.01),
        ):
            with self.assertRaises(RepositoryError):
                reader.resolve_snapshot("demo")

    def test_validator_rejects_sensitive_evidence_path(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/.env", "TOKEN=secret-value\n")
        commit = fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        blob = next(
            entry for entry in reader.list_blobs(snapshot) if entry.path == ".env"
        )
        evidence = SourceEvidence(
            evidence_id="evidence-sensitive",
            tool_call_id="call-sensitive",
            repository_id="demo",
            resolved_commit_sha=commit,
            path=".env",
            excerpt=".env",
            content_hash="sha256:"
            + __import__("hashlib").sha256(b".env").hexdigest(),
            source_blob_id=blob.blob_id,
            extraction_method=ExtractionMethod.TREE,
        )

        with self.assertRaises(EvidenceValidationError):
            DeterministicEvidenceValidator(reader).validate(snapshot, evidence)


if __name__ == "__main__":
    unittest.main()
