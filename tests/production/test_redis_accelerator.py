from prd_agent.production.redis_signals import RedisCancellationSignal


class FakeRedis:
    def __init__(self) -> None:
        self.values = {}
        self.published = []

    def set(self, key, value, *, ex):
        self.values[key] = (value, ex)

    def get(self, key):
        value = self.values.get(key)
        return value[0] if value else None

    def publish(self, channel, value):
        self.published.append((channel, value))


def test_redis_cancellation_is_only_a_ttl_accelerator():
    redis = FakeRedis()
    signal = RedisCancellationSignal(redis, ttl_seconds=300)

    signal.notify("run-1")

    assert signal.is_requested("run-1")
    assert redis.values == {"prd-agent:cancel:run-1": ("1", 300)}
    assert redis.published == [("prd-agent:cancellation", "run-1")]
