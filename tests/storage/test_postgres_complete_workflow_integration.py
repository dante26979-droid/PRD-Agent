import os
import unittest
import uuid

from prd_agent.domain.commands import (
    ConfirmOutline,
    ConfirmUnit,
    FinalizePrd,
    ReopenPrd,
    StartTask,
)
from prd_agent.domain.enums import TaskStatus
from prd_agent.evidence.models import (
    DeterministicFact,
    ExtractionMethod,
    FactType,
    SourceEvidence,
    VerificationStatus,
)
from prd_agent.grounding.service import GroundingService
from prd_agent.quality.service import DocumentQualityService
from prd_agent.storage.postgres import PostgresWorkflowRepository
from prd_agent.workflow.service import WorkflowService
from tests.workflow.support import ScriptedModel, first_outline, sufficient_brief
from tests.workflow.test_complete_workflow import two_unit_outline


@unittest.skipUnless(
    os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    "PRD_AGENT_TEST_DATABASE_DSN is required for PostgreSQL integration tests",
)
class PostgresCompleteWorkflowIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = PostgresWorkflowRepository.from_dsn(
            os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
        )
        self.task_id: str | None = None

    def tearDown(self) -> None:
        if self.task_id:
            with self.repository.connection.cursor() as cursor:
                for table in (
                    "quality_results",
                    "grounding_results",
                    "prd_document_versions",
                    "prd_section_versions",
                    "domain_events",
                    "workflow_checkpoints",
                    "idempotency_records",
                    "agent_runs",
                    "information_needs",
                ):
                    column = "thread_id" if table == "workflow_checkpoints" else "task_id"
                    if table == "workflow_checkpoints":
                        cursor.execute(
                            "DELETE FROM workflow_checkpoints WHERE task_id = %s",
                            (self.task_id,),
                        )
                    else:
                        cursor.execute(
                            f"DELETE FROM {table} WHERE {column} = %s",
                            (self.task_id,),
                        )
                cursor.execute(
                    "DELETE FROM outline_versions WHERE task_id = %s",
                    (self.task_id,),
                )
                cursor.execute(
                    "DELETE FROM requirement_brief_versions WHERE task_id = %s",
                    (self.task_id,),
                )
                cursor.execute(
                    "DELETE FROM task_messages WHERE task_id = %s",
                    (self.task_id,),
                )
                cursor.execute(
                    "DELETE FROM prd_tasks WHERE task_id = %s",
                    (self.task_id,),
                )
            self.repository.connection.commit()
        self.repository.connection.close()

    def test_complete_two_unit_workflow_round_trips_through_postgres(self) -> None:
        suffix = uuid.uuid4().hex
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [two_unit_outline()],
                "generate_confirmation_unit": [
                    {"content": "### 规则\n\n- 支持开始时间和结束时间筛选。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法时间范围时，应只返回范围内订单。"
                        )
                    },
                    {"content": "### 规则\n\n- 支持闭区间时间筛选。"},
                    {
                        "content": (
                            "### 验收\n\n"
                            "- 当输入合法闭区间时，应只返回范围内订单。"
                        )
                    },
                ],
            }
        )
        service = WorkflowService(
            self.repository,
            model,
            quality_service=DocumentQualityService(),
        )
        outline_wait = service.start_task(
            StartTask("订单列表增加创建时间筛选", f"pg-start-{suffix}")
        )
        self.task_id = outline_wait.task.task_id
        first_wait = service.confirm_outline(
            ConfirmOutline(
                self.task_id,
                1,
                outline_wait.task.version,
                f"pg-outline-{suffix}",
            )
        )
        first = first_wait.current_outline.confirmation_units[0]
        second_wait = service.confirm_unit(
            ConfirmUnit(
                self.task_id,
                first.unit_id,
                first_wait.task.version,
                f"pg-first-{suffix}",
            )
        )
        second = second_wait.current_outline.confirmation_units[1]
        review = service.confirm_unit(
            ConfirmUnit(
                self.task_id,
                second.unit_id,
                second_wait.task.version,
                f"pg-second-{suffix}",
            )
        )
        completed = service.finalize_prd(
            FinalizePrd(
                self.task_id,
                review.documents[-1].document_id,
                review.documents[-1].content_hash,
                review.task.version,
                f"pg-finalize-{suffix}",
            )
        )
        reopened = service.reopen_prd(
            ReopenPrd(
                self.task_id,
                (first.unit_id,),
                "筛选规则改为闭区间",
                completed.task.version,
                f"pg-reopen-{suffix}",
            )
        )
        revised_first = reopened.current_outline.confirmation_units[0]
        revised_second_wait = service.confirm_unit(
            ConfirmUnit(
                self.task_id,
                revised_first.unit_id,
                reopened.task.version,
                f"pg-revised-first-{suffix}",
            )
        )
        revised_second = revised_second_wait.current_outline.confirmation_units[1]
        review_v2 = service.confirm_unit(
            ConfirmUnit(
                self.task_id,
                revised_second.unit_id,
                revised_second_wait.task.version,
                f"pg-revised-second-{suffix}",
            )
        )
        completed_v2 = service.finalize_prd(
            FinalizePrd(
                self.task_id,
                review_v2.documents[-1].document_id,
                review_v2.documents[-1].content_hash,
                review_v2.task.version,
                f"pg-finalize-v2-{suffix}",
            )
        )

        restored = service.show(self.task_id)
        self.assertEqual(completed_v2.task.status, TaskStatus.COMPLETED)
        self.assertEqual(restored.task.status, TaskStatus.COMPLETED)
        self.assertEqual(len(restored.current_outline.confirmation_units), 2)
        self.assertEqual(len(restored.sections), 4)
        self.assertEqual(len(restored.documents), 2)
        self.assertTrue(restored.quality_results[-1].confirmable)
        self.assertEqual(
            restored.documents[-1].content_hash,
            completed_v2.documents[-1].content_hash,
        )

    def test_grounding_result_and_evidence_appendix_round_trip_through_postgres(
        self,
    ) -> None:
        suffix = uuid.uuid4().hex
        commit = "a" * 40
        evidence = SourceEvidence(
            evidence_id=f"evidence-{suffix}",
            tool_call_id=f"call-{suffix}",
            repository_id="demo",
            resolved_commit_sha=commit,
            path="src/orders/filter.py",
            line_start=10,
            line_end=14,
            excerpt="订单列表支持开始时间筛选。",
            content_hash="sha256:filter",
            extraction_method=ExtractionMethod.SOURCE_READ,
        )
        fact = DeterministicFact(
            fact_id=f"fact-{suffix}",
            tool_call_id=f"call-{suffix}",
            subject="订单列表",
            predicate="支持",
            value_json="开始时间筛选",
            fact_type=FactType.CODE_VERIFIED,
            confidence="HIGH",
            verification_status=VerificationStatus.SUPPORTED,
            extractor_id="test",
            extractor_version="1",
            evidence_ids=(evidence.evidence_id,),
        )
        model = ScriptedModel(
            {
                "extract_requirement_brief": [sufficient_brief()],
                "generate_outline": [first_outline()],
                "generate_confirmation_unit": [
                    {
                        "content": "### 当前能力\n\n当前订单列表支持开始时间筛选。",
                        "claims": [
                            {
                                "claim_id": f"claim-{suffix}",
                                "text": "当前订单列表支持开始时间筛选。",
                                "kind": "CURRENT_STATE",
                                "criticality": "CRITICAL",
                                "fact_ids": [fact.fact_id],
                            }
                        ],
                    }
                ],
            }
        )

        def context_provider(**kwargs):
            return {
                "repository_id": "demo",
                "resolved_commit_sha": commit,
                "facts": [fact.model_copy(update={"task_id": kwargs["task"].task_id})],
                "evidence": [evidence],
                "conflicts": [],
            }

        service = WorkflowService(
            self.repository,
            model,
            unit_context_provider=context_provider,
            grounding_service=GroundingService(),
        )
        outline_wait = service.start_task(
            StartTask("增加开始时间筛选", f"pg-ground-start-{suffix}")
        )
        self.task_id = outline_wait.task.task_id
        unit_wait = service.confirm_outline(
            ConfirmOutline(
                self.task_id,
                1,
                outline_wait.task.version,
                f"pg-ground-outline-{suffix}",
            )
        )
        unit = unit_wait.current_outline.confirmation_units[0]
        review = service.confirm_unit(
            ConfirmUnit(
                self.task_id,
                unit.unit_id,
                unit_wait.task.version,
                f"pg-ground-unit-{suffix}",
            )
        )

        restored = service.show(self.task_id)
        self.assertEqual(len(restored.grounding_results), 1)
        self.assertTrue(restored.grounding_results[0].confirmable)
        self.assertIn("src/orders/filter.py:10-14", restored.markdown)
        self.assertEqual(restored.markdown, review.markdown)


if __name__ == "__main__":
    unittest.main()
