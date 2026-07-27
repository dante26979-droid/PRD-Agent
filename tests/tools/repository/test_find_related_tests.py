import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.find_related_tests import (
    FindRelatedTestsArguments,
    FindRelatedTestsTool,
)

from tests.repository.support import TemporaryGitRepository


class FindRelatedTestsTests(unittest.TestCase):
    def test_symbol_match_ranks_before_module_keyword_match(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/domain/order.py", "def transition_order(): pass\n")
        fixture.write(
            "demo/tests/test_order.py",
            "def test_transition():\n    transition_order()\n",
        )
        fixture.write("demo/tests/test_checkout.py", "# order transition scenario\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = FindRelatedTestsTool(reader, ToolLimitPolicy()).execute(
            snapshot,
            FindRelatedTestsArguments(
                source_path="src/domain/order.py",
                symbol="transition_order",
                keywords=("transition",),
            ),
        )

        self.assertEqual(result.items[0].path, "tests/test_order.py")
        self.assertEqual(result.items[0].metadata["reason"], "symbol")
        self.assertGreater(
            result.items[0].metadata["score"], result.items[1].metadata["score"]
        )


if __name__ == "__main__":
    unittest.main()
