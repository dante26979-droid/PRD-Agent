import unittest

from prd_agent.domain.enums import TaskStatus
from prd_agent.domain.errors import InvalidTransition, VersionConflict
from prd_agent.policies.task_policy import TaskTransitionPolicy


class TaskTransitionPolicyTests(unittest.TestCase):
    def test_allows_declared_transition_and_rejects_model_selected_state(self) -> None:
        policy = TaskTransitionPolicy()

        self.assertEqual(
            policy.transition(
                TaskStatus.DRAFT,
                TaskStatus.CLARIFYING,
                current_version=1,
                expected_version=1,
            ),
            TaskStatus.CLARIFYING,
        )

        with self.assertRaises(InvalidTransition):
            policy.transition(
                TaskStatus.DRAFT,
                TaskStatus.COMPLETED,
                current_version=1,
                expected_version=1,
            )

    def test_rejects_stale_task_version(self) -> None:
        with self.assertRaises(VersionConflict):
            TaskTransitionPolicy().transition(
                TaskStatus.OUTLINE_REVIEW,
                TaskStatus.GENERATING,
                current_version=3,
                expected_version=2,
            )


if __name__ == "__main__":
    unittest.main()
