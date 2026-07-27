from __future__ import annotations

import argparse
import os
import signal
from threading import Event

from prd_agent.production.config import secret_or_environment
from prd_agent.production.dispatch import RunReconciler
from prd_agent.production.postgres_dispatch import PostgresProductionControlStore


def reconcile_once() -> int:
    store = PostgresProductionControlStore.from_dsn(
        secret_or_environment("PRD_AGENT_DATABASE_DSN")
    )
    try:
        values = RunReconciler(store).reconcile()
        return len(values)
    finally:
        store.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PRD Agent run reconciler")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.once:
        reconcile_once()
        return 0
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        reconcile_once()
        stopping.wait(max(args.interval, 0.5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
