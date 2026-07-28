import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.repo_tree import RepoTreeArguments, RepoTreeTool

from tests.repository.support import TemporaryGitRepository


class RepoTreeTests(unittest.TestCase):
    def test_returns_stable_sorted_scope_relative_paths(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/z.py", "z = 1\n")
        fixture.write("demo/src/a.py", "a = 1\n")
        fixture.write("outside/secret.py", "SECRET = 1\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = RepoTreeTool(reader, ToolLimitPolicy()).execute(
            snapshot, RepoTreeArguments(prefix="", max_depth=5)
        )

        self.assertEqual(result.status, ToolStatus.SUCCEEDED)
        self.assertEqual([item.path for item in result.items], ["src/a.py", "z.py"])
        self.assertTrue(all("outside" not in item.path for item in result.items))

    def test_blocks_sensitive_prefix_before_listing(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/.env", "TOKEN=secret-value\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )

        result = RepoTreeTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"), RepoTreeArguments(prefix=".env")
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOCKED_PATH")
        self.assertEqual(result.items, ())


if __name__ == "__main__":
    unittest.main()
