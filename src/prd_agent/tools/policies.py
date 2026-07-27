from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolLimitPolicy:
    version: str = "repository-tools.v1"
    max_tree_entries: int = 1000
    max_search_candidate_files: int = 300
    max_search_scanned_bytes: int = 5 * 1024 * 1024
    max_search_results: int = 100
    max_read_lines: int = 200
    max_excerpt_bytes: int = 16 * 1024
    max_parser_input_bytes: int = 1024 * 1024
    max_related_tests: int = 20

