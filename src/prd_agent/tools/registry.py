from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from prd_agent.hashing import sha256_json

from .models import ToolAction


class ToolRegistryError(Exception):
    pass


@dataclass(frozen=True)
class ValidatedToolAction:
    action: ToolAction
    arguments: BaseModel
    handler: object


@dataclass(frozen=True)
class _Registration:
    arguments_model: type[BaseModel]
    handler: object


def action_signature(action: ToolAction) -> str:
    return sha256_json(
        {
            "tool_id": action.tool_id,
            "tool_schema_version": action.tool_schema_version,
            "repository_id": action.repository_id,
            "resolved_commit_sha": action.resolved_commit_sha,
            "arguments": action.arguments,
        }
    )


class ToolRegistry:
    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], _Registration] = {}

    def register(
        self,
        tool_id: str,
        schema_version: str,
        arguments_model: type[BaseModel],
        handler: object,
    ) -> None:
        key = (tool_id, schema_version)
        if key in self._registrations:
            raise ToolRegistryError(f"tool is already registered: {tool_id}@{schema_version}")
        self._registrations[key] = _Registration(arguments_model, handler)

    def validate(self, action: ToolAction) -> ValidatedToolAction:
        registration = self._registrations.get(
            (action.tool_id, action.tool_schema_version)
        )
        if not registration:
            raise ToolRegistryError(
                f"tool is not registered: {action.tool_id}@{action.tool_schema_version}"
            )
        arguments = registration.arguments_model.model_validate(action.arguments)
        return ValidatedToolAction(action, arguments, registration.handler)

    def describe(self) -> tuple[dict, ...]:
        """Return public, versioned schemas safe to expose to an action selector."""
        return tuple(
            {
                "tool_id": tool_id,
                "tool_schema_version": schema_version,
                "arguments_schema": registration.arguments_model.model_json_schema(),
            }
            for (tool_id, schema_version), registration in sorted(
                self._registrations.items()
            )
        )
