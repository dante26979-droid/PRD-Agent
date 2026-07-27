import unittest

from prd_agent.investigation.models import (
    InformationNeed,
    InvestigationBudget,
    ProposedAction,
    StopReason,
)
from prd_agent.investigation.planner import ScriptedActionSelector

from tests.investigation.support import build_services
from tests.repository.support import TemporaryGitRepository


def need(*coverage):
    return InformationNeed(
        information_need_id="need-1",
        question="当前订单金额如何存储？",
        requiredness="REQUIRED",
        source_types=("CODE",),
        required_coverage=coverage,
        trigger_stage="UNIT_PREPARATION",
        fallback="ASK_USER_OR_MARK_UNKNOWN",
        planner_version="v1",
        context_hash="sha256:test",
    )


class InvestigationRunnerTests(unittest.TestCase):
    def test_parser_action_completes_required_coverage(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write(
            "demo/db/schema.sql",
            "CREATE TABLE orders (amount NUMERIC(12, 2) NOT NULL);\n",
        )
        fixture.commit()
        selector = ScriptedActionSelector(
            [
                ProposedAction(
                    tool_id="parse_database_schema",
                    arguments={"path": "db/schema.sql", "table": "orders"},
                    purpose="确认订单金额存储格式",
                    target_coverage=("storage_schema",),
                )
            ]
        )
        snapshot, evidence_store, store, application = build_services(fixture, selector)
        investigation = application.create(
            need("storage_schema"),
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        result = application.run(investigation.investigation_id)

        self.assertEqual(result.status.value, "COMPLETE")
        self.assertEqual(result.stop_reason, StopReason.COVERAGE_COMPLETE)
        self.assertEqual(result.coverage["storage_schema"].status.value, "COVERED")
        self.assertEqual(len(result.fact_ids), 1)
        self.assertEqual(evidence_store.all_calls()[0].investigation_id, investigation.investigation_id)
        self.assertEqual(store.get_investigation(investigation.investigation_id).tool_call_count, 1)

    def test_repeated_empty_action_stops_without_second_physical_call(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()
        action = ProposedAction(
            tool_id="search_text",
            arguments={"query": "does-not-exist"},
            purpose="查找不存在的字段",
            target_coverage=("validation_logic",),
        )
        snapshot, evidence_store, store, application = build_services(
            fixture, ScriptedActionSelector([action])
        )
        investigation = application.create(
            need("validation_logic"),
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        result = application.run(investigation.investigation_id)

        self.assertEqual(result.status.value, "EMPTY")
        self.assertEqual(result.stop_reason, StopReason.NO_PROGRESS)
        self.assertEqual(len(evidence_store.all_calls()), 1)
        saved = store.get_investigation(investigation.investigation_id)
        self.assertEqual(saved.replan_count, 1)
        self.assertEqual(saved.tool_call_count, 1)
        self.assertTrue(result.unknown_ids)

    def test_iteration_budget_is_a_deterministic_stop(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()
        selector = ScriptedActionSelector(
            [
                ProposedAction(
                    tool_id="search_text",
                    arguments={"query": "amount"},
                    purpose="定位金额规则",
                    target_coverage=("validation_logic",),
                )
            ]
        )
        snapshot, _, store, application = build_services(fixture, selector)
        investigation = application.create(
            need("validation_logic"),
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            budget=InvestigationBudget(max_iterations=1),
        )

        result = application.run(investigation.investigation_id)

        self.assertEqual(result.status.value, "PARTIAL")
        self.assertEqual(result.stop_reason, StopReason.MAX_ITERATIONS_REACHED)
        self.assertEqual(store.get_investigation(investigation.investigation_id).tool_call_count, 1)

    def test_cancelled_investigation_does_not_execute_tool(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()
        selector = ScriptedActionSelector(
            [
                ProposedAction(
                    tool_id="search_text",
                    arguments={"query": "amount"},
                    purpose="定位金额规则",
                    target_coverage=("validation_logic",),
                )
            ]
        )
        snapshot, evidence_store, _, application = build_services(fixture, selector)
        investigation = application.create(
            need("validation_logic"),
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
        )

        result = application.runner.cancel(investigation.investigation_id)

        self.assertEqual(result.status.value, "CANCELLED")
        self.assertEqual(result.stop_reason, StopReason.USER_STOPPED)
        self.assertEqual(evidence_store.all_calls(), ())

    def test_selector_token_budget_stops_before_tool_execution(self) -> None:
        fixture = TemporaryGitRepository()
        self.addCleanup(fixture.cleanup)
        fixture.write("demo/src/rules.py", "amount > 0\n")
        fixture.commit()

        class TokenSelector(ScriptedActionSelector):
            def select(self, payload, *, repair=False, replan=False):
                action = super().select(payload, repair=repair, replan=replan)
                self.last_token_usage = 50
                return action

        selector = TokenSelector(
            [
                ProposedAction(
                    tool_id="search_text",
                    arguments={"query": "amount"},
                    purpose="定位金额规则",
                    target_coverage=("validation_logic",),
                )
            ]
        )
        snapshot, evidence_store, store, application = build_services(fixture, selector)
        investigation = application.create(
            need("validation_logic"),
            repository_id="demo",
            resolved_commit_sha=snapshot.resolved_commit_sha,
            budget=InvestigationBudget(token_budget=50),
        )

        result = application.run(investigation.investigation_id)

        self.assertEqual(result.stop_reason, StopReason.TOKEN_BUDGET_EXHAUSTED)
        self.assertEqual(evidence_store.all_calls(), ())
        self.assertEqual(
            store.get_investigation(investigation.investigation_id).token_usage, 50
        )


if __name__ == "__main__":
    unittest.main()
