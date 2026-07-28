import unittest

from pydantic import ValidationError

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.parse_database_schema import (
    ParseDatabaseSchemaArguments,
    ParseDatabaseSchemaTool,
)
from prd_agent.tools.repository.parse_openapi import ParseOpenApiArguments, ParseOpenApiTool
from prd_agent.tools.repository.read_file import ReadFileArguments, ReadFileTool

from tests.repository.support import TemporaryGitRepository


class RepositoryToolErrorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TemporaryGitRepository()
        self.addCleanup(self.fixture.cleanup)

    def _reader(self) -> GitCliObjectReader:
        return GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", self.fixture.root, "demo", "HEAD")])
        )

    def test_read_file_rejects_invalid_range_before_execution(self) -> None:
        self.fixture.write("demo/src/rules.py", "first\nsecond\n")
        self.fixture.commit()

        with self.assertRaises(ValidationError):
            ReadFileArguments(path="src/rules.py", line_start=2, line_end=1)

    def test_read_file_reports_range_limit(self) -> None:
        self.fixture.write("demo/src/rules.py", "first\nsecond\n")
        self.fixture.commit()
        reader = self._reader()

        result = ReadFileTool(reader, ToolLimitPolicy(max_read_lines=1)).execute(
            reader.resolve_snapshot("demo"),
            ReadFileArguments(path="src/rules.py", line_start=1, line_end=2),
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "READ_RANGE_LIMIT")

    def test_read_file_reports_missing_blob_without_fallback(self) -> None:
        self.fixture.write("demo/src/rules.py", "first\n")
        self.fixture.commit()
        reader = self._reader()

        result = ReadFileTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ReadFileArguments(path="src/missing.py", line_start=1, line_end=1),
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOB_NOT_FOUND")
        self.assertEqual(result.items, ())

    def test_read_file_blocks_binary_content(self) -> None:
        target = self.fixture.root / "demo/bin/data.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x01binary")
        self.fixture.commit()
        reader = self._reader()

        result = ReadFileTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ReadFileArguments(path="bin/data.bin", line_start=1, line_end=1),
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOCKED_FILE_TYPE")

    def test_openapi_malformed_document_is_failed(self) -> None:
        self.fixture.write("demo/openapi.yaml", "openapi: [\n")
        self.fixture.commit()
        reader = self._reader()

        result = ParseOpenApiTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"), ParseOpenApiArguments(path="openapi.yaml")
        )

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.error_code, "MALFORMED_OPENAPI")
        self.assertEqual(result.items, ())

    def test_openapi_unsupported_version_is_failed(self) -> None:
        self.fixture.write("demo/openapi.yaml", "openapi: 2.0\npaths: {}\n")
        self.fixture.commit()
        reader = self._reader()

        result = ParseOpenApiTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"), ParseOpenApiArguments(path="openapi.yaml")
        )

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.error_code, "UNSUPPORTED_OPENAPI_VERSION")

    def test_database_schema_malformed_document_is_failed(self) -> None:
        self.fixture.write("demo/db/schema.sql", "CREATE TABLE orders (\n")
        self.fixture.commit()
        reader = self._reader()

        result = ParseDatabaseSchemaTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ParseDatabaseSchemaArguments(path="db/schema.sql"),
        )

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(result.error_code, "MALFORMED_DDL")

    def test_database_schema_mixed_support_is_partial(self) -> None:
        self.fixture.write(
            "demo/db/schema.sql",
            "CREATE TABLE orders (id BIGINT);\nALTER TABLE orders ADD COLUMN total NUMERIC;\n",
        )
        self.fixture.commit()
        reader = self._reader()

        result = ParseDatabaseSchemaTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ParseDatabaseSchemaArguments(path="db/schema.sql"),
        )

        self.assertEqual(result.status, ToolStatus.PARTIAL)
        self.assertEqual(result.error_code, "PARSE_UNSUPPORTED")
        self.assertEqual(len(result.items), 1)


if __name__ == "__main__":
    unittest.main()
