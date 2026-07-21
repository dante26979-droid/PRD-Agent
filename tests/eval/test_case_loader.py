import json
import tempfile
import unittest
from pathlib import Path

from prd_agent.eval.case_loader import DatasetValidationError, load_dataset


class CaseLoaderTests(unittest.TestCase):
    def test_loads_manifest_and_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = root / "cases"
            cases.mkdir()
            (cases / "case-001.json").write_text(
                json.dumps(
                    {
                        "case_id": "case-001",
                        "title": "校验规则变更",
                        "requirement": "支持手机号格式校验",
                        "context": "当前系统使用字符串校验器",
                        "tags": ["required_investigation"],
                        "repository_id": "demo-repo",
                        "resolved_commit_sha": "a" * 40,
                        "expected_information_needs": [
                            {
                                "type": "REQUIRED",
                                "category": "validation_logic",
                                "reason": "需求影响输入约束",
                            }
                        ],
                        "required_sources": [],
                        "expected_facts": [],
                        "expected_unknowns": ["旧数据是否需要回填"],
                        "expected_conflicts": [],
                        "required_prd_sections": [
                            "requirement_summary",
                            "acceptance_criteria",
                        ],
                        "clarification_questions": ["是否兼容历史数据"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "dataset_version": "eval-v1",
                        "repository_id": "demo-repo",
                        "resolved_commit_sha": "a" * 40,
                        "cases": ["cases/case-001.json"],
                    }
                ),
                encoding="utf-8",
            )

            dataset = load_dataset(manifest)

        self.assertEqual(dataset.dataset_version, "eval-v1")
        self.assertEqual(dataset.cases[0].case_id, "case-001")
        self.assertEqual(dataset.cases[0].expected_information_needs[0].kind, "REQUIRED")

    def test_rejects_duplicate_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "case-a.json").write_text(
                json.dumps({"case_id": "same"}), encoding="utf-8"
            )
            (root / "case-b.json").write_text(
                json.dumps({"case_id": "same"}), encoding="utf-8"
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "dataset_version": "eval-v1",
                        "repository_id": "demo-repo",
                        "resolved_commit_sha": "a" * 40,
                        "cases": ["case-a.json", "case-b.json"],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(DatasetValidationError):
                load_dataset(manifest)

    def test_rejects_invalid_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / "case.json"
            case.write_text(json.dumps({"case_id": "case-001"}), encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "dataset_version": "eval-v1",
                        "repository_id": "demo-repo",
                        "resolved_commit_sha": "not-a-sha",
                        "cases": [case.name],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(DatasetValidationError):
                load_dataset(manifest)


if __name__ == "__main__":
    unittest.main()
