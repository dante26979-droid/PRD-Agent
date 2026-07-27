import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.find_symbol import FindSymbolArguments, FindSymbolTool

from tests.repository.support import TemporaryGitRepository


class FindSymbolToolTests(unittest.TestCase):
    def test_finds_python_function_and_constant_definitions(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/order.py", "ORDER_STATES = ()\n\ndef transition_order():\n    pass\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        tool = FindSymbolTool(reader, ToolLimitPolicy())

        result = tool.execute(snapshot, FindSymbolArguments(symbol="transition_order"))

        self.assertEqual(result.status.value, "SUCCEEDED")
        self.assertEqual(result.items[0].path, "src/order.py")
        self.assertEqual(result.items[0].line_start, 3)


if __name__ == "__main__":
    unittest.main()
