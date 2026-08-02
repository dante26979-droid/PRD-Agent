from __future__ import annotations

import hashlib
import json

import pytest

from agent.unit.models import RunOutputKind, RunPurpose, UnitRunResult
from agent.unit.transport import encode_run_output, to_agent_result
from agent.v1 import agent_execution_pb2 as proto


def _result() -> UnitRunResult:
    payload = {"schema_version": "outline-candidate.v1", "title": "Example"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return UnitRunResult(
        purpose=RunPurpose.PLAN_OUTLINE,
        output_kind=RunOutputKind.OUTLINE_CANDIDATE,
        scope_hash="a" * 64,
        content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        payload=payload,
    )


def test_encode_run_output_uses_canonical_payload_and_enum_contract() -> None:
    output = encode_run_output(_result(), output_key="outline:run-1", expected_task_version=3)

    assert output.schema_version == "run-output.v1"
    assert output.output_kind == proto.RUN_OUTPUT_KIND_OUTLINE_CANDIDATE
    assert output.run_purpose == proto.RUN_PURPOSE_PLAN_OUTLINE
    assert output.scope_hash == "a" * 64
    assert output.expected_task_version == 3
    assert json.loads(output.payload) == {"schema_version": "outline-candidate.v1", "title": "Example"}
    assert to_agent_result(
        _result(), output_key="outline:run-1", expected_task_version=3
    ).run_output == output


def test_encode_run_output_rejects_mutated_result_payload() -> None:
    result = _result()
    object.__setattr__(result, "payload", {"changed": True})

    with pytest.raises(ValueError, match="content hash"):
        encode_run_output(result, output_key="outline:run-1", expected_task_version=3)
