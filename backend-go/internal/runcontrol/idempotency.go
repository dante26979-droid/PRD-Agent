package runcontrol

import (
	"crypto/sha256"
	"encoding/hex"
)

// StartTaskRequestHash is persisted instead of the raw user message so an
// idempotency record never becomes an accidental second copy of request data.
func StartTaskRequestHash(tenantID, ownerID, message string) string {
	digest := sha256.Sum256([]byte(tenantID + "\x00" + ownerID + "\x00" + message))
	return hex.EncodeToString(digest[:])
}
