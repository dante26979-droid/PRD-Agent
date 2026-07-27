import pytest

from prd_agent.production.graph_compatibility import (
    CheckpointCompatibilityError,
    GraphVersion,
    build_thread_id,
    validate_checkpoint,
)


def test_thread_id_is_stable_per_graph_major_version():
    assert build_thread_id("task-1", "prd-workflow", 2) == build_thread_id(
        "task-1", "prd-workflow", 2
    )
    assert build_thread_id("task-1", "prd-workflow", 2) != build_thread_id(
        "task-1", "prd-workflow", 3
    )


def test_checkpoint_allows_minor_upgrade_but_rejects_unknown_major():
    checkpoint = {
        "schema_version": 1,
        "graph_name": "prd-workflow",
        "graph_version": "2.1.0",
        "task_id": "task-1",
        "task_version": 7,
        "cursor": "WAIT_USER",
    }

    value = validate_checkpoint(
        checkpoint,
        current=GraphVersion.parse("2.3.0"),
    )

    assert value.cursor == "WAIT_USER"
    with pytest.raises(CheckpointCompatibilityError):
        validate_checkpoint(
            checkpoint,
            current=GraphVersion.parse("3.0.0"),
        )
