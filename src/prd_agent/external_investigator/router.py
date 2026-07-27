from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .models import ExternalInvestigationRequest, ValidatedExternalInvestigation


class InvestigatorProfile(StrEnum):
    NATIVE = "NATIVE"
    EXTERNAL_CODING_AGENT = "EXTERNAL_CODING_AGENT"


@dataclass(frozen=True)
class RoutedInvestigation:
    selected_profile: InvestigatorProfile
    result: ValidatedExternalInvestigation
    fallback_from: InvestigatorProfile | None = None
    fallback_reason: str | None = None


class InvestigationProfileRouter:
    def __init__(self, native_runner, external_runner) -> None:
        self.native_runner = native_runner
        self.external_runner = external_runner

    def run(
        self,
        request: ExternalInvestigationRequest,
        *,
        profile: InvestigatorProfile = InvestigatorProfile.NATIVE,
        allow_fallback: bool = False,
    ) -> RoutedInvestigation:
        profile = InvestigatorProfile(profile)
        if profile == InvestigatorProfile.NATIVE:
            return RoutedInvestigation(
                selected_profile=profile,
                result=self.native_runner.run(request),
            )
        try:
            return RoutedInvestigation(
                selected_profile=profile,
                result=self.external_runner.run(request),
            )
        except Exception:
            if not allow_fallback:
                raise
            return RoutedInvestigation(
                selected_profile=InvestigatorProfile.NATIVE,
                result=self.native_runner.run(request),
                fallback_from=InvestigatorProfile.EXTERNAL_CODING_AGENT,
                fallback_reason="EXTERNAL_PROVIDER_FAILED",
            )
