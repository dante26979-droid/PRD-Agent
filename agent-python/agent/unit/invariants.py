from __future__ import annotations

from agent.unit.models import UnitCandidate, UnitPatch, UnitRunRequest


class UnitInvariantError(ValueError):
    pass


class UnitInvariantPolicy:
    def validate_candidate(
        self,
        request: UnitRunRequest,
        candidate: UnitCandidate,
    ) -> UnitCandidate:
        scope = request.scope
        if candidate.unit_key != scope.current_unit_key:
            raise UnitInvariantError("candidate changed the current unit key")
        if candidate.title != scope.current_unit_title:
            raise UnitInvariantError("candidate title is not from the locked outline")
        if candidate.ordinal != scope.current_unit_ordinal:
            raise UnitInvariantError("candidate changed the locked unit order")
        if candidate.node_keys != scope.section_node_keys:
            raise UnitInvariantError("candidate changed the locked outline nodes")
        immutable = set(scope.immutable_unit_keys)
        if candidate.unit_key in immutable:
            raise UnitInvariantError("candidate attempted to modify an immutable unit")
        self._validate_heading(candidate)
        return candidate

    def validate_patch(
        self,
        request: UnitRunRequest,
        patch: UnitPatch,
    ) -> UnitPatch:
        scope = request.scope
        base = request.base_candidate
        if base is None:
            raise UnitInvariantError("unit patch requires a base candidate")
        if patch.unit_key != scope.current_unit_key or patch.unit_key != base.unit_key:
            raise UnitInvariantError("unit patch escaped the current unit")
        if patch.unit_key not in scope.reopened_unit_keys:
            raise UnitInvariantError("unit patch requires a reopened unit")
        if patch.unit_key in scope.immutable_unit_keys:
            raise UnitInvariantError("unit patch attempted to modify an immutable unit")
        if patch.base_content_hash.removeprefix("sha256:") != base.content_hash:
            raise UnitInvariantError("unit patch base hash is stale")
        unresolved = set(request.unresolved_unknown_ids)
        if not unresolved.issubset(set(patch.preserved_unknown_ids)):
            raise UnitInvariantError("unit patch removed unresolved unknowns")
        if not set(patch.used_fact_ids).issubset(set(request.available_fact_ids)):
            raise UnitInvariantError("unit patch referenced unavailable facts")
        self._validate_heading(
            UnitCandidate(
                unit_key=base.unit_key,
                title=base.title,
                ordinal=base.ordinal,
                node_keys=base.node_keys,
                markdown=patch.replacement_markdown,
            )
        )
        return patch

    @staticmethod
    def immutable_hashes(request: UnitRunRequest) -> dict[str, str]:
        immutable = set(request.scope.immutable_unit_keys)
        return {
            item.unit_key: item.content_hash.removeprefix("sha256:")
            for item in request.scope.confirmed_context
            if item.unit_key in immutable
        }

    @staticmethod
    def _validate_heading(candidate: UnitCandidate) -> None:
        headings = [
            line.lstrip("#").strip()
            for line in candidate.markdown.splitlines()
            if line.startswith("#")
        ]
        if not headings or headings[0] != candidate.title:
            raise UnitInvariantError("unit markdown must start with its locked title")
