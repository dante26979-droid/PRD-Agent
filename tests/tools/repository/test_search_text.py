import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.search_text import SearchTextArguments, SearchTextTool

from tests.repository.support import TemporaryGitRepository


class SearchTextTests(unittest.TestCase):
    def test_literal_search_returns_exact_sorted_locator(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/src/validators.py",
            "def validate():\n    raise ValueError('unsupported currency')\n",
        )
        fixture.write("demo/tests/test_validation.py", "# unsupported currency\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = SearchTextTool(reader, ToolLimitPolicy()).execute(
            snapshot,
            SearchTextArguments(query="unsupported currency", prefix="src"),
        )

        self.assertEqual(result.status, ToolStatus.SUCCEEDED)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].path, "src/validators.py")
        self.assertEqual(result.items[0].line_start, 2)
        self.assertEqual(result.items[0].column, 23)
        self.assertIn("unsupported currency", result.items[0].excerpt)

    def test_marks_partial_at_exact_result_limit(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/a.py", "amount one\namount two\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        limits = ToolLimitPolicy(max_search_results=1)

        result = SearchTextTool(reader, limits).execute(
            reader.resolve_snapshot("demo"), SearchTextArguments(query="amount")
        )

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.truncation.reason, "RESULT_LIMIT")

    def test_blocks_sensitive_prefix_before_scanning(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/.env", "TOKEN=secret-value\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )

        result = SearchTextTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"), SearchTextArguments(query="TOKEN", prefix=".env")
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOCKED_PATH")
        self.assertEqual(result.items, ())


if __name__ == "__main__":
    unittest.main()
