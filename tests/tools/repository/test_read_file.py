import unittest
import os

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.read_file import ReadFileArguments, ReadFileTool

from tests.repository.support import TemporaryGitRepository


class ReadFileTests(unittest.TestCase):
    def test_reads_only_explicit_one_based_line_range(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "first\nsecond\nthird\nfourth\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = ReadFileTool(reader, ToolLimitPolicy()).execute(
            snapshot, ReadFileArguments(path="src/rules.py", line_start=2, line_end=3)
        )

        self.assertEqual(result.status, ToolStatus.SUCCEEDED)
        self.assertEqual(result.items[0].line_start, 2)
        self.assertEqual(result.items[0].line_end, 3)
        self.assertEqual(result.items[0].excerpt, "second\nthird")
        self.assertNotIn("first", result.items[0].excerpt)

    def test_blocks_git_symlink_entry(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/target.txt", "safe target\n")
        os.symlink("target.txt", fixture.root / "demo/link.txt")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        result = ReadFileTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ReadFileArguments(path="link.txt", line_start=1, line_end=1),
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOCKED_PATH")

    def test_redacts_secret_before_constructing_tool_result(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/config.py", 'token = "super-secret-value"\n')
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        result = ReadFileTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ReadFileArguments(path="src/config.py", line_start=1, line_end=1),
        )

        serialized = result.model_dump_json()
        self.assertNotIn("super-secret-value", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertTrue(result.items[0].metadata["redaction_applied"])


if __name__ == "__main__":
    unittest.main()
