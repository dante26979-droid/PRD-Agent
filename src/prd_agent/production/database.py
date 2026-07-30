"""Connection-pool and request-scoped repository lifecycle."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable


class RequestConnectionScope:
    """Own one pooled connection for a request or worker unit.

    Nested store bindings reuse the current connection so repositories that
    participate in one application unit cannot accidentally observe different
    transaction snapshots.
    """

    def __init__(self, pool) -> None:
        self.pool = pool
        self._current: ContextVar[object | None] = ContextVar(
            f"connection-{id(self)}",
            default=None,
        )

    @contextmanager
    def bind(self):
        current = self._current.get()
        if current is not None:
            yield current
            return
        with self.pool.connection() as connection:
            token = self._current.set(connection)
            try:
                yield connection
            finally:
                self._current.reset(token)


class RequestScopedRepository:
    """Delegate repository calls to the connection bound to this request."""

    def __init__(self, pool_or_scope, repository_factory: Callable[[object], object]) -> None:
        self.scope = (
            pool_or_scope
            if isinstance(pool_or_scope, RequestConnectionScope)
            else RequestConnectionScope(pool_or_scope)
        )
        self.pool = self.scope.pool
        self.repository_factory = repository_factory
        self._current: ContextVar[object | None] = ContextVar(
            f"repository-{id(self)}",
            default=None,
        )

    @contextmanager
    def bind(self):
        with self.scope.bind() as connection:
            repository = self.repository_factory(connection)
            token = self._current.set(repository)
            try:
                yield repository
            finally:
                self._current.reset(token)

    def __getattr__(self, name: str):
        current = self._current.get()
        if current is None:
            raise RuntimeError("repository used outside a request or worker unit")
        return getattr(current, name)


class ProductionDatabase:
    def __init__(self, pool) -> None:
        self.pool = pool
        self.scope = RequestConnectionScope(pool)

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
        statement_timeout_ms: int = 30_000,
        lock_timeout_ms: int = 5_000,
        idle_transaction_timeout_ms: int = 30_000,
    ) -> "ProductionDatabase":
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Connection pooling requires: "
                "python -m pip install '.[production]'"
            ) from exc
        options = " ".join(
            (
                f"-c statement_timeout={statement_timeout_ms}",
                f"-c lock_timeout={lock_timeout_ms}",
                (
                    "-c idle_in_transaction_session_timeout="
                    f"{idle_transaction_timeout_ms}"
                ),
            )
        )
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"options": options},
            check=ConnectionPool.check_connection,
            name="prd-agent-api",
        )
        return cls(pool)

    def open(self) -> None:
        self.pool.open()
        self.pool.wait()

    def close(self) -> None:
        self.pool.close()

    def scoped_repository(self, repository_factory) -> RequestScopedRepository:
        return RequestScopedRepository(self.scope, repository_factory)

    def readiness(self, *, expected_migration: str) -> bool:
        try:
            with self.pool.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.execute(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM schema_migrations WHERE version = %s
                        )
                        """,
                        (expected_migration,),
                    )
                    return bool(cursor.fetchone()[0])
        except Exception:
            return False
