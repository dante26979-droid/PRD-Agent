import unittest

from prd_agent.investigation.models import Investigation, InvestigationBudget
from prd_agent.investigation.policies import CoverageTemplatePolicy
from prd_agent.storage.postgres_investigation import PostgresInvestigationStore


class PostgresInvestigationContractTests(unittest.TestCase):
    def test_serialized_database_row_round_trips_domain_state(self) -> None:
        investigation = Investigation(
            investigation_id="investigation-1",
            information_need_id="need-1",
            repository_id="demo",
            resolved_commit_sha="a" * 40,
            coverage=CoverageTemplatePolicy().build(("api_contract", "tests")),
            budget=InvestigationBudget(max_iterations=3),
            completed_action_signatures=frozenset({"sha256:action"}),
            evidence_ids=("evidence-1",),
            version=4,
        )

        row = PostgresInvestigationStore._investigation_values(investigation)
        restored = PostgresInvestigationStore._investigation_from_row(row)

        self.assertEqual(restored, investigation)


if __name__ == "__main__":
    unittest.main()
