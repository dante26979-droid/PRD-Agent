package transport

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/dante26979-droid/prd-agent/backend-go/internal/runcontrol"
	"github.com/redis/go-redis/v9"
)

const AgentRunStream = "prd-agent:agent-runs"

// RedisStreamPublisher is only a wake-up transport. PostgreSQL remains the
// source of truth and the outbox row is marked published only after XAdd
// succeeds.
type RedisStreamPublisher struct {
	client *redis.Client
}

func NewRedisStreamPublisher(brokerURL string) (*RedisStreamPublisher, error) {
	options, err := redis.ParseURL(brokerURL)
	if err != nil {
		return nil, fmt.Errorf("parse redis URL: %w", err)
	}
	return &RedisStreamPublisher{client: redis.NewClient(options)}, nil
}

func (p *RedisStreamPublisher) Close() error { return p.client.Close() }

func (p *RedisStreamPublisher) Ping(ctx context.Context) error {
	return p.client.Ping(ctx).Err()
}

func (p *RedisStreamPublisher) Publish(ctx context.Context, message runcontrol.OutboxMessage) error {
	values := map[string]any{
		"message_id":   message.MessageID,
		"aggregate_id": message.AggregateID,
		"event_type":   message.EventType,
		"payload":      json.RawMessage(message.Payload),
	}
	return p.client.XAdd(ctx, &redis.XAddArgs{Stream: AgentRunStream, Values: values}).Err()
}
