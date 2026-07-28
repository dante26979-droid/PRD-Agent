import unittest

from prd_agent.tools.models import ToolAction
from prd_agent.tools.registry import ToolRegistry, action_signature
from prd_agent.tools.repository.read_file import ReadFileArguments


class ToolRegistryTests(unittest.TestCase):
    def test_signature_is_canonical_and_excludes_purpose(self) -> None:
        first = ToolAction(
            tool_id="read_file",
            tool_schema_version="1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            arguments={"path": "src/a.py", "line_start": 1, "line_end": 2},
            purpose="确认规则",
        )
        second = first.model_copy(
            update={
                "arguments": {"line_end": 2, "line_start": 1, "path": "src/a.py"},
                "purpose": "另一种公开说明",
            }
        )

        self.assertEqual(action_signature(first), action_signature(second))

        registry = ToolRegistry()
        registry.register("read_file", "1", ReadFileArguments, object())
        validated = registry.validate(first)
        self.assertIsInstance(validated.arguments, ReadFileArguments)

        description = registry.describe()
        self.assertEqual(description[0]["tool_id"], "read_file")
        self.assertEqual(description[0]["tool_schema_version"], "1")
        self.assertIn("properties", description[0]["arguments_schema"])


if __name__ == "__main__":
    unittest.main()
