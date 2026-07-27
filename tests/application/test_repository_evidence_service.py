import unittest

from prd_agent.application.repository_evidence_service import (
    IdempotencyConflict,
    RepositoryEvidenceService,
)
from prd_agent.evidence.deterministic_validator import DeterministicEvidenceValidator
from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.storage.memory_evidence import InMemoryEvidenceStore
from prd_agent.tools.models import ToolAction, ToolStatus
from prd_agent.tools.default_registry import build_repository_tool_registry
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.registry import ToolRegistry
from prd_agent.tools.repository.read_file import ReadFileArguments, ReadFileTool
from prd_agent.tools.repository.search_text import SearchTextArguments, SearchTextTool
from prd_agent.tools.repository.parse_database_schema import (
    ParseDatabaseSchemaArguments,
    ParseDatabaseSchemaTool,
)

from tests.repository.support import TemporaryGitRepository


class RepositoryEvidenceServiceTests(unittest.TestCase):
    def test_execute_is_idempotent_and_returns_verified_evidence_bundle(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        limits = ToolLimitPolicy()
        registry = ToolRegistry()
        registry.register(
            "read_file", "1", ReadFileArguments, ReadFileTool(reader, limits)
        )
        store = InMemoryEvidenceStore()
        service = RepositoryEvidenceService(
            reader, registry, store, DeterministicEvidenceValidator(reader)
        )
        action = ToolAction(
            tool_id="read_file",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"path": "src/rules.py", "line_start": 1, "line_end": 1},
            purpose="确认金额规则",
        )

        first = service.execute(action, actor_id="local", idempotency_key="read-1")
        replay = service.execute(action, actor_id="local", idempotency_key="read-1")

        self.assertEqual(first.tool_call.tool_call_id, replay.tool_call.tool_call_id)
        self.assertEqual(first.tool_result.status, ToolStatus.SUCCEEDED)
        self.assertEqual(len(first.bundle.evidence), 1)
        self.assertEqual(len(store.all_calls()), 1)

    def test_empty_search_creates_unknown_not_negative_fact(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        registry = ToolRegistry()
        registry.register(
            "search_text",
            "1",
            SearchTextArguments,
            SearchTextTool(reader, ToolLimitPolicy()),
        )
        service = RepositoryEvidenceService(
            reader,
            registry,
            InMemoryEvidenceStore(),
            DeterministicEvidenceValidator(reader),
        )
        action = ToolAction(
            tool_id="search_text",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"query": "does-not-exist"},
            purpose="确认是否有匹配",
        )

        execution = service.execute(action, actor_id="local", idempotency_key="empty")

        self.assertEqual(execution.tool_result.status, ToolStatus.EMPTY)
        self.assertEqual(execution.bundle.facts, ())
        self.assertEqual(len(execution.bundle.unknowns), 1)
        self.assertIn("不能据此断言", execution.bundle.unknowns[0].statement)

    def test_validated_parser_output_becomes_supported_fact(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/db/schema.sql",
            "CREATE TABLE orders (\n amount NUMERIC(12, 2) NOT NULL\n);\n",
        )
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        registry = ToolRegistry()
        registry.register(
            "parse_database_schema",
            "1",
            ParseDatabaseSchemaArguments,
            ParseDatabaseSchemaTool(reader, ToolLimitPolicy()),
        )
        service = RepositoryEvidenceService(
            reader,
            registry,
            InMemoryEvidenceStore(),
            DeterministicEvidenceValidator(reader),
        )
        action = ToolAction(
            tool_id="parse_database_schema",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"path": "db/schema.sql", "table": "orders"},
            purpose="确认金额存储类型",
        )

        execution = service.execute(action, actor_id="local", idempotency_key="ddl")

        self.assertEqual(execution.tool_result.status, ToolStatus.SUCCEEDED)
        self.assertEqual(len(execution.bundle.facts), 1)
        self.assertEqual(execution.bundle.facts[0].subject, "orders.amount")

    def test_same_idempotency_key_with_different_input_is_rejected(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "first\nsecond\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        registry = ToolRegistry()
        registry.register(
            "read_file", "1", ReadFileArguments, ReadFileTool(reader, ToolLimitPolicy())
        )
        service = RepositoryEvidenceService(
            reader,
            registry,
            InMemoryEvidenceStore(),
            DeterministicEvidenceValidator(reader),
        )
        first = ToolAction(
            tool_id="read_file",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"path": "src/rules.py", "line_start": 1, "line_end": 1},
            purpose="读取第一行",
        )
        second = first.model_copy(
            update={"arguments": {"path": "src/rules.py", "line_start": 2, "line_end": 2}}
        )
        service.execute(first, actor_id="local", idempotency_key="same")

        with self.assertRaises(IdempotencyConflict):
            service.execute(second, actor_id="local", idempotency_key="same")

    def test_unexpected_tool_failure_has_failed_terminal_record_and_no_evidence(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "first\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        class FailingTool:
            def execute(self, snapshot, arguments):
                raise RuntimeError("provider details must not escape")

        registry = ToolRegistry()
        registry.register("read_file", "1", ReadFileArguments, FailingTool())
        store = InMemoryEvidenceStore()
        service = RepositoryEvidenceService(
            reader, registry, store, DeterministicEvidenceValidator(reader)
        )
        action = ToolAction(
            tool_id="read_file",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"path": "src/rules.py", "line_start": 1, "line_end": 1},
            purpose="读取规则",
        )

        execution = service.execute(action, actor_id="local", idempotency_key="failure")

        self.assertEqual(execution.tool_result.status, ToolStatus.FAILED)
        self.assertEqual(execution.tool_result.error_code, "TOOL_EXECUTION_FAILED")
        self.assertEqual(execution.bundle.evidence, ())
        self.assertNotIn("provider details", execution.tool_result.model_dump_json())

    def test_processing_failure_has_failed_terminal_record_and_no_evidence(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "first\n")
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")
        registry = ToolRegistry()
        registry.register(
            "read_file", "1", ReadFileArguments, ReadFileTool(reader, ToolLimitPolicy())
        )
        store = InMemoryEvidenceStore()
        service = RepositoryEvidenceService(
            reader, registry, store, DeterministicEvidenceValidator(reader)
        )

        class ExplodingNormalizer:
            def normalize(self, *args, **kwargs):
                raise RuntimeError("internal provider details")

        service.normalizer = ExplodingNormalizer()
        action = ToolAction(
            tool_id="read_file",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            arguments={"path": "src/rules.py", "line_start": 1, "line_end": 1},
            purpose="读取规则",
        )

        execution = service.execute(action, actor_id="local", idempotency_key="processing")

        self.assertEqual(execution.tool_result.status, ToolStatus.FAILED)
        self.assertEqual(execution.tool_result.error_code, "TOOL_PROCESSING_FAILED")
        self.assertEqual(execution.bundle.evidence, ())
        self.assertNotIn("internal provider details", execution.tool_result.model_dump_json())
        self.assertEqual(store.all_calls()[0].status.value, "FAILED")

    def test_all_parser_results_are_redacted_at_the_service_boundary(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/db/schema.sql",
            "CREATE TABLE credentials (\n"
            " password TEXT DEFAULT 'ddl-super-secret'\n"
            ");\n",
        )
        fixture.write(
            "demo/openapi.yaml",
            "openapi: 3.0.0\n"
            "paths:\n"
            "  /login:\n"
            "    post:\n"
            "      requestBody:\n"
            "        content:\n"
            "          application/json:\n"
            "            schema:\n"
            "              type: object\n"
            "              properties:\n"
            "                token:\n"
            "                  type: string\n"
            "                  enum: [openapi-super-secret]\n",
        )
        fixture.write("demo/src/order.py", "def authenticate(): pass\n")
        fixture.write(
            "demo/tests/test_order.py",
            "def test_authenticate():\n"
            "    password='test-super-secret'\n",
        )
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog(
                [RepositoryBinding("demo", fixture.root, "demo", "HEAD")]
            )
        )
        snapshot = reader.resolve_snapshot("demo")
        service = RepositoryEvidenceService(
            reader,
            build_repository_tool_registry(reader),
            InMemoryEvidenceStore(),
            DeterministicEvidenceValidator(reader),
        )
        actions = (
            (
                "parse_database_schema",
                {"path": "db/schema.sql", "table": "credentials"},
            ),
            (
                "parse_openapi",
                {"path": "openapi.yaml", "operation": "POST /login"},
            ),
            (
                "find_related_tests",
                {
                    "source_path": "src/order.py",
                    "symbol": "password",
                },
            ),
        )

        for index, (tool_id, arguments) in enumerate(actions):
            execution = service.execute(
                ToolAction(
                    tool_id=tool_id,
                    tool_schema_version="1",
                    repository_id="demo",
                    resolved_commit_sha=snapshot.resolved_commit_sha,
                    arguments=arguments,
                    purpose="验证统一脱敏边界",
                ),
                actor_id="local",
                idempotency_key=f"redaction-{index}",
            )
            public = execution.model_dump_json()
            self.assertNotIn("super-secret", public)
            self.assertIn("[REDACTED]", public)


if __name__ == "__main__":
    unittest.main()
