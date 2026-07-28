import unittest

from prd_agent.cli import build_parser


class CliTests(unittest.TestCase):
    def test_finalize_command_accepts_document_version_and_hash(self) -> None:
        args = build_parser().parse_args(
            [
                "workflow",
                "finalize",
                "--task-id",
                "task-1",
                "--document-id",
                "document-1",
                "--content-hash",
                "sha256:abc",
                "--expected-task-version",
                "7",
                "--idempotency-key",
                "finalize-1",
            ]
        )

        self.assertEqual(args.workflow_command, "finalize")
        self.assertEqual(args.document_id, "document-1")
        self.assertEqual(args.content_hash, "sha256:abc")

    def test_reopen_command_accepts_units_and_reason(self) -> None:
        args = build_parser().parse_args(
            [
                "workflow",
                "reopen",
                "--task-id",
                "task-1",
                "--unit-id",
                "unit-1",
                "--unit-id",
                "unit-2",
                "--reason",
                "规则变化",
                "--expected-task-version",
                "8",
            ]
        )

        self.assertEqual(args.workflow_command, "reopen")
        self.assertEqual(args.unit_id, ["unit-1", "unit-2"])
        self.assertEqual(args.reason, "规则变化")


if __name__ == "__main__":
    unittest.main()
