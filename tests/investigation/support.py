from prd_agent.application.investigation_service import InvestigationApplicationService
from prd_agent.application.repository_evidence_service import RepositoryEvidenceService
from prd_agent.evidence.deterministic_validator import DeterministicEvidenceValidator
from prd_agent.investigation.runner import InvestigationRunner
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.storage.memory_evidence import InMemoryEvidenceStore
from prd_agent.storage.memory_investigation import InMemoryInvestigationStore
from prd_agent.tools.default_registry import build_repository_tool_registry


def build_services(fixture, selector):
    reader = GitCliObjectReader(
        RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
    )
    snapshot = reader.resolve_snapshot("demo")
    evidence_store = InMemoryEvidenceStore()
    evidence_service = RepositoryEvidenceService(
        reader,
        build_repository_tool_registry(reader),
        evidence_store,
        DeterministicEvidenceValidator(reader),
    )
    investigation_store = InMemoryInvestigationStore()
    runner = InvestigationRunner(
        evidence_service, investigation_store, selector
    )
    application = InvestigationApplicationService(investigation_store, runner)
    return snapshot, evidence_store, investigation_store, application
