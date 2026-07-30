package runcontrol

import (
	"crypto/sha256"
	"encoding/hex"
	"strconv"
)

// StartTaskRequestHash is persisted instead of the raw user message so an
// idempotency record never becomes an accidental second copy of request data.
func StartTaskRequestHash(tenantID, ownerID, message string) string {
	digest := sha256.Sum256([]byte(tenantID + "\x00" + ownerID + "\x00" + message))
	return hex.EncodeToString(digest[:])
}

func RetryTaskRequestHash(tenantID, ownerID, taskID string, expectedTaskVersion int) string {
	digest := sha256.Sum256([]byte(
		tenantID + "\x00" + ownerID + "\x00" + taskID + "\x00" + strconv.Itoa(expectedTaskVersion),
	))
	return hex.EncodeToString(digest[:])
}
