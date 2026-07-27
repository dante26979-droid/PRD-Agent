import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader

from .support import TemporaryGitRepository


class RepositorySnapshotTests(unittest.TestCase):
    def test_snapshot_reads_fixed_commit_not_changed_worktree(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "RULE = 'committed'\n")
        commit = fixture.commit()
        catalog = RepositoryCatalog(
            [RepositoryBinding("demo", fixture.root, "demo", "HEAD")]
        )
        reader = GitCliObjectReader(catalog)

        snapshot = reader.resolve_snapshot("demo")
        fixture.write("demo/src/rules.py", "RULE = 'worktree'\n")
        blob = reader.read_blob(snapshot, "src/rules.py")

        self.assertEqual(snapshot.resolved_commit_sha, commit)
        self.assertEqual(blob.text, "RULE = 'committed'\n")
        self.assertNotIn(str(fixture.root), blob.path)


if __name__ == "__main__":
    unittest.main()
