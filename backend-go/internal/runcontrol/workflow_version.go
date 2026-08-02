package runcontrol

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

type WorkflowVersion string

const (
	WorkflowVersionV1 WorkflowVersion = "agent-runtime.v1"
	WorkflowVersionV4 WorkflowVersion = "agent-runtime.v4"
)

func ParseWorkflowVersion(value string) (WorkflowVersion, error) {
	version := WorkflowVersion(value)
	if !version.Supported() {
		return "", fmt.Errorf("unsupported workflow version %q", value)
	}
	return version, nil
}

func (version WorkflowVersion) Supported() bool {
	return version == WorkflowVersionV1 || version == WorkflowVersionV4
}

func DefaultWorkflowVersion(version WorkflowVersion) WorkflowVersion {
	if version.Supported() {
		return version
	}
	return WorkflowVersionV1
}

func EvidenceReference(item EvidenceItem) string {
	digest := sha256.Sum256([]byte(strings.Join([]string{item.SourceType, item.SourceID, item.Locator, item.ExcerptHash}, "\x00")))
	return "sha256:" + hex.EncodeToString(digest[:])
}
