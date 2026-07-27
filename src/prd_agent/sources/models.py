from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from prd_agent.hashing import sha256_json
from prd_agent.tools.models import ToolAction


class SourceKind(StrEnum):
    CODE_REPOSITORY = "CODE_REPOSITORY"
    HISTORICAL_PRD_CORPUS = "HISTORICAL_PRD_CORPUS"
    HISTORICAL_PRD_DOCUMENT = "HISTORICAL_PRD"


def access_scope_hash(
    owner_id: str,
    project_id: str,
    access_labels: tuple[str, ...] | list[str] = (),
) -> str:
    """Hash a normalized authorization scope without exposing its labels."""
    return sha256_json(
        {
            "owner_id": owner_id,
            "project_id": project_id,
            "access_labels": sorted(set(access_labels)),
        }
    )


class SourceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_id: str = Field(min_length=1)
    source_kind: SourceKind
    source_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    access_scope_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    metadata: dict[str, str] = Field(default_factory=dict)


class ReadAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    tool_schema_version: str = Field(min_length=1)
    source_binding: SourceBinding
    arguments: dict[str, Any]
    purpose: str = Field(min_length=1, max_length=500)


def read_action_signature(action: ReadAction) -> str:
    return sha256_json(
        {
            "tool_id": action.tool_id,
            "tool_schema_version": action.tool_schema_version,
            "source_binding": action.source_binding.model_dump(mode="json"),
            "arguments": action.arguments,
        }
    )


class RepositoryActionAdapter:
    """Preserve the public ToolAction contract while using generic read actions."""

    @staticmethod
    def to_read_action(
        action: ToolAction,
        *,
        owner_id: str,
        project_id: str,
        access_labels: tuple[str, ...] = (),
    ) -> ReadAction:
        scope_hash = access_scope_hash(owner_id, project_id, access_labels)
        return ReadAction(
            tool_id=action.tool_id,
            tool_schema_version=action.tool_schema_version,
            source_binding=SourceBinding(
                binding_id=sha256_json(
                    {
                        "kind": SourceKind.CODE_REPOSITORY,
                        "repository_id": action.repository_id,
                        "commit": action.resolved_commit_sha,
                        "scope": scope_hash,
                    }
                ),
                source_kind=SourceKind.CODE_REPOSITORY,
                source_id=action.repository_id,
                source_version=action.resolved_commit_sha,
                owner_id=owner_id,
                access_scope_hash=scope_hash,
                metadata={"project_id": project_id},
            ),
            arguments=action.arguments,
            purpose=action.purpose,
        )

    @staticmethod
    def to_tool_action(action: ReadAction) -> ToolAction:
        binding = action.source_binding
        if binding.source_kind != SourceKind.CODE_REPOSITORY:
            raise ValueError("only code repository read actions can use ToolAction")
        return ToolAction(
            tool_id=action.tool_id,
            tool_schema_version=action.tool_schema_version,
            repository_id=binding.source_id,
            resolved_commit_sha=binding.source_version,
            arguments=action.arguments,
            purpose=action.purpose,
        )
