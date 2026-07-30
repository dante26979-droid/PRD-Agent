from __future__ import annotations

from contextlib import contextmanager

import pytest

from prd_agent.production.database import (
    ProductionDatabase,
    RequestScopedRepository,
)


class FakeConnection:
    def __init__(self, name: str) -> None:
        self.name = name


class FakePool:
    def __init__(self) -> None:
        self.counter = 0

    @contextmanager
    def connection(self):
        self.counter += 1
        yield FakeConnection(f"connection-{self.counter}")


class FakeRepository:
    def __init__(self, connection) -> None:
        self.connection = connection

    def identity(self) -> str:
        return self.connection.name


def test_request_scoped_repository_uses_a_distinct_connection_per_binding():
    repository = RequestScopedRepository(FakePool(), FakeRepository)

    with pytest.raises(RuntimeError, match="outside a request"):
        repository.identity()
    with repository.bind():
        first = repository.identity()
        assert repository.connection.name == first
    with repository.bind():
        second = repository.identity()

    assert first == "connection-1"
    assert second == "connection-2"


def test_scoped_stores_share_one_connection_inside_the_same_request_unit():
    pool = FakePool()
    database = ProductionDatabase(pool)
    workflow = database.scoped_repository(FakeRepository)
    evidence = database.scoped_repository(FakeRepository)

    with workflow.bind():
        with evidence.bind():
            assert workflow.connection is evidence.connection
            assert pool.counter == 1

    with workflow.bind():
        assert workflow.connection.name == "connection-2"
