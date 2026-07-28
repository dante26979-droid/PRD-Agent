import hashlib
import unittest

from prd_agent.evidence.deterministic_validator import (
    DeterministicEvidenceValidator,
    EvidenceValidationError,
)
from prd_agent.evidence.models import ExtractionMethod, SourceEvidence
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader

from tests.repository.support import TemporaryGitRepository


class DeterministicEvidenceValidatorTests(unittest.TestCase):
    def test_rejects_tampered_excerpt_hash(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "first\namount > 0\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        blob = reader.read_blob(snapshot, "src/rules.py")
        evidence = SourceEvidence(
            evidence_id="evidence-1",
            tool_call_id="call-1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            path="src/rules.py",
            line_start=2,
            line_end=2,
            excerpt="amount >= 0",
            content_hash="sha256:" + hashlib.sha256(b"amount >= 0").hexdigest(),
            source_blob_id=blob.blob_id,
            extraction_method=ExtractionMethod.SOURCE_READ,
        )

        with self.assertRaises(EvidenceValidationError):
            DeterministicEvidenceValidator(reader).validate(snapshot, evidence)


if __name__ == "__main__":
    unittest.main()
