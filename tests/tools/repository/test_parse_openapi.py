import json
import unittest

from prd_agent.repository.bindings import RepositoryBinding, RepositoryCatalog
from prd_agent.repository.git_cli_reader import GitCliObjectReader
from prd_agent.tools.models import ToolStatus
from prd_agent.tools.policies import ToolLimitPolicy
from prd_agent.tools.repository.parse_openapi import (
    ParseOpenApiArguments,
    ParseOpenApiTool,
)

from tests.repository.support import TemporaryGitRepository


class ParseOpenApiTests(unittest.TestCase):
    def test_extracts_request_field_contract_as_deterministic_candidate(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        document = {
            "openapi": "3.0.3",
            "paths": {
                "/orders": {
                    "post": {
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["amount"],
                                        "properties": {
                                            "amount": {
                                                "type": "number",
                                                "format": "decimal",
                                                "minimum": 0,
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    }
                }
            },
        }
        fixture.write("demo/openapi.json", json.dumps(document, indent=2))
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )
        snapshot = reader.resolve_snapshot("demo")

        result = ParseOpenApiTool(reader, ToolLimitPolicy()).execute(
            snapshot, ParseOpenApiArguments(path="openapi.json", operation="POST /orders")
        )

        self.assertEqual(result.status, ToolStatus.SUCCEEDED)
        amount = next(item for item in result.items if item.metadata["field"] == "amount")
        self.assertEqual(amount.kind, "openapi_field")
        self.assertEqual(amount.metadata["type"], "number")
        self.assertEqual(amount.metadata["format"], "decimal")
        self.assertTrue(amount.metadata["required"])
        self.assertEqual(amount.metadata["minimum"], 0)
        self.assertGreater(amount.line_start, 0)

    def test_blocks_external_reference_without_fetching_network(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/openapi.yaml",
            """openapi: 3.0.3
paths:
  /orders:
    post:
      requestBody:
        $ref: https://example.com/order.yaml
""",
        )
        fixture.commit()
        reader = GitCliObjectReader(
            RepositoryCatalog([RepositoryBinding("demo", fixture.root, "demo", "HEAD")])
        )

        result = ParseOpenApiTool(reader, ToolLimitPolicy()).execute(
            reader.resolve_snapshot("demo"),
            ParseOpenApiArguments(path="openapi.yaml"),
        )

        self.assertEqual(result.status, ToolStatus.BLOCKED)
        self.assertEqual(result.error_code, "BLOCKED_EXTERNAL_REFERENCE")
        self.assertEqual(result.items, ())


if __name__ == "__main__":
    unittest.main()
