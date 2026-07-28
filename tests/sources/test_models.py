from prd_agent.sources.models import (
    RepositoryActionAdapter,
    access_scope_hash,
    read_action_signature,
)
from prd_agent.tools.models import ToolAction
from prd_agent.tools.registry import action_signature


def test_repository_action_adapter_round_trips_without_changing_legacy_signature():
    action = ToolAction(
        tool_id="read_file",
        tool_schema_version="1",
        repository_id="demo",
        resolved_commit_sha="a" * 40,
        arguments={"path": "README.md"},
        purpose="读取说明",
    )
    legacy_signature = action_signature(action)

    adapted = RepositoryActionAdapter.to_read_action(
        action,
        owner_id="owner-1",
        project_id="project-1",
        access_labels=("team",),
    )

    assert RepositoryActionAdapter.to_tool_action(adapted) == action
    assert action_signature(action) == legacy_signature
    assert read_action_signature(adapted).startswith("sha256:")


def test_access_scope_and_read_signature_are_order_independent_but_permission_bound():
    assert access_scope_hash("owner", "project", ["b", "a", "a"]) == (
        access_scope_hash("owner", "project", ["a", "b"])
    )
    assert access_scope_hash("owner", "project", ["a"]) != (
        access_scope_hash("owner", "project", ["a", "secret"])
    )
