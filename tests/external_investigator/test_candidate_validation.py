from __future__ import annotations

from prd_agent.external_investigator.models import (
    CandidateEvidenceLocator,
    ExternalInvestigationRequest,
    ExternalInvestigationResult,
)
from prd_agent.external_investigator.service import ExternalInvestigationService
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader

from tests.repository.support import TemporaryGitRepository


class FakeExternalAgent:
    def run(self, request):
        return ExternalInvestigationResult(
            status="COMPLETE",
            candidates=(
                CandidateEvidenceLocator(
                    path="src/rules.py",
                    line_start=2,
                    line_end=2,
                    symbol="is_paid",
                    candidate_claim="订单状态使用 paid",
                ),
            ),
            coverage={"validation_logic": "COVERED"},
            unknowns=(),
            action_trace=(),
        )


def test_external_candidate_is_reread_from_the_fixed_commit_before_becoming_evidence():
    fixture = TemporaryGitRepository()
    try:
        fixture.write(
            "demo/src/rules.py",
            "def is_paid(order):\n    return order.status == 'paid'\n",
        )
        commit = fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog(
                [RepositoryBinding("demo", fixture.root, "demo", commit)]
            )
        )
        service = ExternalInvestigationService(reader, FakeExternalAgent())

        result = service.run(
            ExternalInvestigationRequest(
                investigation_id="investigation-1",
                repository_id="demo",
                resolved_commit_sha=commit,
                question="订单完成状态是什么？",
                required_coverage={"validation_logic": "MISSING"},
                allowed_path_prefixes=("src",),
                max_candidates=5,
                max_total_bytes=10_000,
                timeout_seconds=30,
            )
        )

        assert result.status == "COMPLETE"
        assert len(result.evidence) == 1
        evidence = result.evidence[0]
        assert evidence.resolved_commit_sha == commit
        assert evidence.path == "src/rules.py"
        assert evidence.excerpt == "    return order.status == 'paid'"
        assert result.rejected_candidates == 0
    finally:
        fixture.cleanup()


def test_hallucinated_external_locator_is_rejected_without_creating_evidence():
    class HallucinatingAgent:
        def run(self, request):
            return ExternalInvestigationResult(
                status="COMPLETE",
                candidates=(
                    CandidateEvidenceLocator(
                        path="src/missing.py",
                        line_start=1,
                        line_end=1,
                        candidate_claim="不存在的代码结论",
                    ),
                ),
            )

    fixture = TemporaryGitRepository()
    try:
        fixture.write("demo/src/rules.py", "STATUS = 'paid'\n")
        commit = fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog(
                [RepositoryBinding("demo", fixture.root, "demo", commit)]
            )
        )

        result = ExternalInvestigationService(reader, HallucinatingAgent()).run(
            ExternalInvestigationRequest(
                investigation_id="investigation-2",
                repository_id="demo",
                resolved_commit_sha=commit,
                question="状态是什么？",
                required_coverage={"validation_logic": "MISSING"},
                allowed_path_prefixes=("src",),
            )
        )

        assert result.status == "PARTIAL"
        assert result.evidence == ()
        assert result.rejected_candidates == 1
    finally:
        fixture.cleanup()


def test_external_candidate_uses_the_same_secret_redaction_boundary():
    class SecretFindingAgent:
        def run(self, request):
            return ExternalInvestigationResult(
                status="COMPLETE",
                candidates=(
                    CandidateEvidenceLocator(
                        path="src/config.py",
                        line_start=1,
                        line_end=1,
                    ),
                ),
            )

    fixture = TemporaryGitRepository()
    try:
        fixture.write("demo/src/config.py", 'API_KEY = "live-secret-value"\n')
        commit = fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog(
                [RepositoryBinding("demo", fixture.root, "demo", commit)]
            )
        )

        result = ExternalInvestigationService(reader, SecretFindingAgent()).run(
            ExternalInvestigationRequest(
                investigation_id="investigation-secret",
                repository_id="demo",
                resolved_commit_sha=commit,
                question="配置是什么？",
                required_coverage={},
                allowed_path_prefixes=("src",),
            )
        )

        assert result.evidence[0].excerpt.startswith("[REDACTED]")
        assert result.evidence[0].redaction_applied is True
        assert "live-secret-value" not in str(result)
    finally:
        fixture.cleanup()
