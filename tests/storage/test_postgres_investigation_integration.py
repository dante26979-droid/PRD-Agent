import os
import unittest
import uuid

from prd_agent.investigation.models import (
    InformationNeed,
    Investigation,
    InvestigationStatus,
    InvestigationStep,
)
from prd_agent.investigation.policies import CoverageTemplatePolicy
from prd_agent.storage.postgres_investigation import PostgresInvestigationStore


@unittest.skipUnless(
    os.environ.get("PRD_AGENT_TEST_DATABASE_DSN"),
    "PRD_AGENT_TEST_DATABASE_DSN is required for PostgreSQL integration tests",
)
class PostgresInvestigationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PostgresInvestigationStore.from_dsn(
            os.environ["PRD_AGENT_TEST_DATABASE_DSN"]
        )
        suffix = uuid.uuid4().hex
        self.need_id = f"test-need-{suffix}"
        self.investigation_id = f"test-investigation-{suffix}"

    def tearDown(self) -> None:
        with self.store.connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM investigation_steps WHERE investigation_id = %s",
                (self.investigation_id,),
            )
            cursor.execute(
                "DELETE FROM investigations WHERE investigation_id = %s",
                (self.investigation_id,),
            )
            cursor.execute(
                "DELETE FROM information_needs WHERE information_need_id = %s",
                (self.need_id,),
            )
        self.store.connection.commit()
        self.store.connection.close()

    def test_round_trip_and_optimistic_update(self) -> None:
        need = InformationNeed(
            information_need_id=self.need_id,
            question="当前金额字段如何存储？",
            requiredness="REQUIRED",
            source_types=("CODE",),
            required_coverage=("storage_schema",),
            trigger_stage="TEST",
            fallback="ASK_USER",
            planner_version="test.v1",
            context_hash="sha256:test",
        )
        self.store.save_need(need)
        investigation = Investigation(
            investigation_id=self.investigation_id,
            information_need_id=self.need_id,
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            coverage=CoverageTemplatePolicy().build(("storage_schema",)),
        )
        self.store.create_investigation(investigation)
        running = investigation.model_copy(
            update={"status": InvestigationStatus.RUNNING, "version": 2}
        )
        self.store.save_investigation(running, expected_version=1)
        self.store.append_step(
            InvestigationStep(
                step_id=f"test-step-{uuid.uuid4().hex}",
                investigation_id=self.investigation_id,
                sequence=1,
                iteration=0,
                step_type="START",
                status="SUCCEEDED",
                public_summary="调查已开始",
                input_hash="sha256:test",
            )
        )

        restored = self.store.get_investigation(self.investigation_id)

        self.assertEqual(restored.status, InvestigationStatus.RUNNING)
        self.assertEqual(restored.version, 2)
        self.assertEqual(len(self.store.steps(self.investigation_id)), 1)


if __name__ == "__main__":
    unittest.main()
