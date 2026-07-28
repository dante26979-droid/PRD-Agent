package id

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
)

// New returns an opaque identifier with a stable resource prefix.
func New(prefix string) (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("generate %s id: %w", prefix, err)
	}
	return prefix + "-" + hex.EncodeToString(raw[:]), nil
}
