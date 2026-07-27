from __future__ import annotations

from .policies import ToolLimitPolicy
from .registry import ToolRegistry
from .repository.find_related_tests import FindRelatedTestsArguments, FindRelatedTestsTool
from .repository.find_references import FindReferencesArguments, FindReferencesTool
from .repository.find_symbol import FindSymbolArguments, FindSymbolTool
from .repository.parse_database_schema import (
    ParseDatabaseSchemaArguments,
    ParseDatabaseSchemaTool,
)
from .repository.parse_openapi import ParseOpenApiArguments, ParseOpenApiTool
from .repository.read_file import ReadFileArguments, ReadFileTool
from .repository.repo_tree import RepoTreeArguments, RepoTreeTool
from .repository.search_text import SearchTextArguments, SearchTextTool


def build_repository_tool_registry(
    reader, limits: ToolLimitPolicy | None = None
) -> ToolRegistry:
    limits = limits or ToolLimitPolicy()
    registry = ToolRegistry()
    registry.register("repo_tree", "1", RepoTreeArguments, RepoTreeTool(reader, limits))
    registry.register(
        "search_text", "1", SearchTextArguments, SearchTextTool(reader, limits)
    )
    registry.register("read_file", "1", ReadFileArguments, ReadFileTool(reader, limits))
    registry.register(
        "parse_openapi",
        "1",
        ParseOpenApiArguments,
        ParseOpenApiTool(reader, limits),
    )
    registry.register(
        "parse_database_schema",
        "1",
        ParseDatabaseSchemaArguments,
        ParseDatabaseSchemaTool(reader, limits),
    )
    registry.register(
        "find_related_tests",
        "1",
        FindRelatedTestsArguments,
        FindRelatedTestsTool(reader, limits),
    )
    registry.register(
        "find_symbol", "1", FindSymbolArguments, FindSymbolTool(reader, limits)
    )
    registry.register(
        "find_references",
        "1",
        FindReferencesArguments,
        FindReferencesTool(reader, limits),
    )
    return registry
