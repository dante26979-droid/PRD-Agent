from prd_agent.external_investigator.models import (
    ExternalInvestigationRequest,
    ValidatedExternalInvestigation,
)
from prd_agent.external_investigator.router import (
    InvestigationProfileRouter,
    InvestigatorProfile,
)


def request():
    return ExternalInvestigationRequest(
        investigation_id="investigation-1",
        repository_id="demo",
        resolved_commit_sha="a" * 40,
        question="状态是什么？",
        required_coverage={},
    )


class Runner:
    def __init__(self, name, *, fail=False):
        self.name = name
        self.fail = fail
        self.calls = 0

    def run(self, value):
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider failed")
        return ValidatedExternalInvestigation(
            investigation_id=value.investigation_id,
            status="EMPTY",
            unknowns=(self.name,),
        )


def test_router_defaults_to_native_and_makes_external_fallback_explicit():
    native = Runner("native")
    external = Runner("external", fail=True)
    router = InvestigationProfileRouter(native, external)

    default = router.run(request())
    fallback = router.run(
        request(),
        profile=InvestigatorProfile.EXTERNAL_CODING_AGENT,
        allow_fallback=True,
    )

    assert default.selected_profile == "NATIVE"
    assert default.result.unknowns == ("native",)
    assert fallback.selected_profile == "NATIVE"
    assert fallback.fallback_from == "EXTERNAL_CODING_AGENT"
    assert fallback.fallback_reason == "EXTERNAL_PROVIDER_FAILED"
    assert native.calls == 2
    assert external.calls == 1
