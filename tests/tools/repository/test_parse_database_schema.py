import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.parse_database_schema import (
    ParseDatabaseSchemaArguments,
    ParseDatabaseSchemaTool,
)

from tests.repository.support import TemporaryGitRepository


class ParseDatabaseSchemaTests(unittest.TestCase):
    def test_extracts_postgres_column_type_null_default_and_check(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/db/schema.sql",
            """CREATE TABLE orders (
    id BIGSERIAL PRIMARY KEY,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    status TEXT NOT NULL DEFAULT 'pending'
);
""",
        )
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = ParseDatabaseSchemaTool(reader, ToolLimitPolicy()).execute(
            snapshot,
            ParseDatabaseSchemaArguments(path="db/schema.sql", table="orders"),
        )

        self.assertEqual(result.status, ToolStatus.SUCCEEDED)
        amount = next(item for item in result.items if item.metadata["column"] == "amount")
        self.assertEqual(amount.metadata["type"], "DECIMAL(12, 2)")
        self.assertFalse(amount.metadata["nullable"])
        self.assertEqual(amount.metadata["checks"], ["amount > 0"])
        status = next(item for item in result.items if item.metadata["column"] == "status")
        self.assertEqual(status.metadata["default"], "'pending'")


if __name__ == "__main__":
    unittest.main()
