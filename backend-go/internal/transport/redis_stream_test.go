package transport

import (
	"bytes"
	"testing"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
)

func TestRedisStreamValuesUseRedisSupportedPayloadType(t *testing.T) {
	payload := []byte(`{"run_id":"run-1"}`)
	values := redisStreamValues(runcontrol.OutboxMessage{
		MessageID:   "outbox-run-1",
		AggregateID: "run-1",
		EventType:   "agent.run.requested",
		Payload:     payload,
	})

	encoded, ok := values["payload"].([]byte)
	if !ok {
		t.Fatalf("payload type = %T, want []byte accepted by go-redis", values["payload"])
	}
	if !bytes.Equal(encoded, payload) {
		t.Fatalf("payload = %q, want %q", encoded, payload)
	}
}
