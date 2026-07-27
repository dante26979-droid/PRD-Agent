"""Redis accelerators; PostgreSQL remains the source of business truth."""

from __future__ import annotations


class RedisCancellationSignal:
    def __init__(
        self,
        redis_client,
        *,
        ttl_seconds: int = 300,
        namespace: str = "prd-agent",
    ) -> None:
        self.redis_client = redis_client
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace

    def notify(self, run_id: str) -> None:
        self.redis_client.set(
            f"{self.namespace}:cancel:{run_id}",
            "1",
            ex=self.ttl_seconds,
        )
        self.redis_client.publish(
            f"{self.namespace}:cancellation",
            run_id,
        )

    def is_requested(self, run_id: str) -> bool:
        return (
            self.redis_client.get(f"{self.namespace}:cancel:{run_id}")
            is not None
        )
