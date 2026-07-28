from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def postgres_checkpointer(dsn: str):
    """Yield the official LangGraph PostgreSQL checkpointer after schema setup."""

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError as exc:  # pragma: no cover - optional integration boundary
        raise RuntimeError(
            "LangGraph PostgreSQL support requires: python -m pip install '.[workflow]'"
        ) from exc
    with PostgresSaver.from_conn_string(dsn) as checkpointer:
        checkpointer.setup()
        yield checkpointer
