from __future__ import annotations

from agent.v1 import agent_execution_pb2 as proto


def artifact_index(items: tuple[proto.RunArtifact, ...]) -> dict[str, proto.RunArtifact]:
    index: dict[str, proto.RunArtifact] = {}
    for item in items:
        previous = index.setdefault(item.artifact_key, item)
        if previous.content_hash != item.content_hash or previous.request_hash != item.request_hash:
            raise ValueError("conflicting durable outcome artifacts")
    return index


def ledger_index(items: tuple[proto.RunLedgerEntry, ...]) -> dict[str, proto.RunLedgerEntry]:
    index: dict[str, proto.RunLedgerEntry] = {}
    for item in items:
        previous = index.setdefault(item.operation_key, item)
        if previous.request_hash != item.request_hash or previous.entry_kind != item.entry_kind or previous.operation != item.operation:
            raise ValueError("conflicting durable ledger identities")
    return index
