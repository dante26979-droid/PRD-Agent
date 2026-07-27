from __future__ import annotations

from typing import Protocol

from .models import ExternalInvestigationRequest, ExternalInvestigationResult


class ExternalCodingAgentAdapter(Protocol):
    def run(
        self,
        request: ExternalInvestigationRequest,
    ) -> ExternalInvestigationResult: ...
