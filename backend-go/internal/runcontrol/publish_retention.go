package runcontrol

import (
	"context"
	"time"
)

const PublishPayloadRetentionWindow = 24 * time.Hour

type PublishPayloadLease struct {
	PublishID   string    `json:"publish_id"`
	ContentHash string    `json:"content_hash"`
	WorkerID    string    `json:"worker_id"`
	ExpiresAt   time.Time `json:"expires_at"`
}

type PublishPayloadRetention interface {
	ClaimExpiredPublishPayloads(ctx context.Context, workerID string, now time.Time, limit int, ttl time.Duration) ([]PublishPayloadLease, error)
	PurgePublishPayload(ctx context.Context, lease PublishPayloadLease, now time.Time) error
}

func (s *MemoryStore) ClaimExpiredPublishPayloads(_ context.Context, workerID string, now time.Time, limit int, ttl time.Duration) ([]PublishPayloadLease, error) {
	if workerID == "" {
		return nil, ErrInvalidPayload
	}
	if limit < 1 {
		limit = 1
	}
	if ttl <= 0 {
		ttl = time.Minute
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	items := make([]PublishPayloadLease, 0, limit)
	for publishID, value := range s.publishes {
		terminal := value.record.Status == PublishSucceeded || value.record.Status == PublishManualReview
		if !terminal || len(value.content) == 0 || value.record.UpdatedAt.Add(PublishPayloadRetentionWindow).After(now) ||
			(value.retentionWorker != "" && value.retentionExpires.After(now)) {
			continue
		}
		value.retentionWorker = workerID
		value.retentionExpires = now.Add(ttl)
		s.publishes[publishID] = value
		items = append(items, PublishPayloadLease{PublishID: publishID, ContentHash: value.record.ContentHash, WorkerID: workerID, ExpiresAt: value.retentionExpires})
		if len(items) == limit {
			break
		}
	}
	return items, nil
}

func (s *MemoryStore) PurgePublishPayload(_ context.Context, lease PublishPayloadLease, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	value, ok := s.publishes[lease.PublishID]
	if !ok {
		return ErrNotFound
	}
	if len(value.content) == 0 && !value.payloadPurgedAt.IsZero() {
		return nil
	}
	if value.retentionWorker != lease.WorkerID || value.retentionExpires != lease.ExpiresAt || !value.retentionExpires.After(now) || value.record.ContentHash != lease.ContentHash {
		return ErrLeaseLost
	}
	value.content = nil
	value.payloadPurgedAt = now
	value.retentionWorker = ""
	value.retentionExpires = time.Time{}
	s.publishes[lease.PublishID] = value
	return nil
}

var _ PublishPayloadRetention = (*MemoryStore)(nil)
