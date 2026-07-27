from prd_agent.external_investigator.ablation import (
    ExternalInvestigatorAblation,
    InvestigatorEvalCase,
)
from prd_agent.external_investigator.models import (
    ExternalInvestigationRequest,
    ValidatedExternalInvestigation,
)


def evidence(path, line):
    from prd_agent.evidence.models import ExtractionMethod, SourceEvidence

    return SourceEvidence(
        evidence_id=f"evidence-{path}-{line}",
        tool_call_id="eval",
        repository_id="demo",
        resolved_commit_sha="a" * 40,
        path=path,
        line_start=line,
        line_end=line,
        excerpt="value",
        content_hash="sha256:" + "1" * 64,
        extraction_method=ExtractionMethod.EXTERNAL_AGENT_CANDIDATE,
    )


class Runner:
    def __init__(self, items):
        self.items = items

    def run(self, request):
        return ValidatedExternalInvestigation(
            investigation_id=request.investigation_id,
            status="COMPLETE",
            evidence=tuple(self.items),
        )


def test_ablation_compares_profiles_against_the_same_fixed_locator_truth():
    request = ExternalInvestigationRequest(
        investigation_id="eval-investigation",
        repository_id="demo",
        resolved_commit_sha="a" * 40,
        question="状态在哪里？",
        required_coverage={},
    )
    case = InvestigatorEvalCase(
        case_id="case-1",
        request=request,
        expected_locators=(("src/rules.py", 2, 2),),
    )

    report = ExternalInvestigatorAblation(
        Runner([evidence("src/rules.py", 2)]),
        Runner([evidence("src/wrong.py", 1), evidence("src/rules.py", 2)]),
    ).run((case,))

    assert report.native.evidence_precision == 1.0
    assert report.native.evidence_recall == 1.0
    assert report.external.evidence_precision == 0.5
    assert report.external.evidence_recall == 1.0
    assert report.case_ids == ("case-1",)
