from __future__ import annotations

import argparse
import os
import signal
from threading import Event

from prd_agent.production.config import secret_or_environment
from prd_agent.production.dispatch import OutboxPublisher
from prd_agent.production.postgres_dispatch import PostgresProductionControlStore
from prd_agent.production.queueing import CeleryMessageBroker, build_celery_app


def publish_once() -> int:
    dsn = secret_or_environment("PRD_AGENT_DATABASE_DSN")
    broker_url = os.environ["PRD_AGENT_BROKER_URL"]
    store = PostgresProductionControlStore.from_dsn(dsn)
    try:
        broker = CeleryMessageBroker(
            build_celery_app(broker_url=broker_url)
        )
        return OutboxPublisher(
            store,
            broker,
            publisher_id=os.environ.get(
                "PRD_AGENT_INSTANCE_ID",
                f"publisher-{os.getpid()}",
            ),
        ).publish_once()
    finally:
        store.connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PRD Agent Outbox publisher")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.once:
        publish_once()
        return 0
    stopping = Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        publish_once()
        stopping.wait(max(args.interval, 0.1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
