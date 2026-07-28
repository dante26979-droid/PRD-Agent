import unittest

from prd_agent.domain.entities import AgentRun
from prd_agent.domain.enums import RunStatus
from prd_agent.domain.errors import InvalidTransition
from prd_agent.policies.run_policy import RunPolicy


class RunPolicyTests(unittest.TestCase):
    def test_only_failed_or_stopped_run_is_retryable(self) -> None:
        policy = RunPolicy()
        policy.ensure_retryable(AgentRun("failed", "task", "thread", status=RunStatus.FAILED))

        with self.assertRaises(InvalidTransition):
            policy.ensure_retryable(
                AgentRun("waiting", "task", "thread", status=RunStatus.WAITING_USER)
            )


if __name__ == "__main__":
    unittest.main()
