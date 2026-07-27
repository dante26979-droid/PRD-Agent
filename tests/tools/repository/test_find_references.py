import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.find_references import (
    FindReferencesArguments,
    FindReferencesTool,
)

from tests.repository.support import TemporaryGitRepository


class FindReferencesToolTests(unittest.TestCase):
    def test_finds_references_but_excludes_definition(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/src/order.py",
            "def transition_order():\n    pass\n\ntransition_order()\n",
        )
        fixture.write(
            "demo/tests/test_order.py",
            "from src.order import transition_order\ntransition_order()\n",
        )
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        tool = FindReferencesTool(reader, ToolLimitPolicy())

        result = tool.execute(
            snapshot, FindReferencesArguments(symbol="transition_order")
        )

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(len(result.items), 3)
        self.assertNotIn("def transition_order", [item.excerpt for item in result.items])


if __name__ == "__main__":
    unittest.main()
