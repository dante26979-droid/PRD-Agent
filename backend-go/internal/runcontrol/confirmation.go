package runcontrol

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
)

type ConfirmationCandidate struct {
	UnitKey     string   `json:"unit_key"`
	Title       string   `json:"title"`
	Ordinal     int      `json:"order"`
	Markdown    string   `json:"markdown"`
	ContentHash string   `json:"content_hash"`
	ClaimIDs    []string `json:"claim_ids"`
	UnknownIDs  []string `json:"unknown_ids"`
	DependsOn   []string `json:"depends_on"`
}

func ParseConfirmationCandidates(patch []byte, taskID, runID string) ([]ConfirmationCandidate, bool, error) {
	var document struct {
		SchemaVersion     string                  `json:"schema_version"`
		TaskID            string                  `json:"task_id"`
		RunID             string                  `json:"run_id"`
		ConfirmationUnits []ConfirmationCandidate `json:"confirmation_units"`
	}
	if err := json.Unmarshal(patch, &document); err != nil {
		return nil, false, nil
	}
	if document.SchemaVersion == "" {
		return nil, false, nil
	}
	if document.SchemaVersion != "working-draft.v2" {
		return nil, true, fmt.Errorf("%w: unsupported working draft schema", ErrInvalidPayload)
	}
	if document.TaskID != taskID || document.RunID != runID || len(document.ConfirmationUnits) == 0 {
		return nil, true, fmt.Errorf("%w: v2 draft requires task, run and confirmation units", ErrInvalidPayload)
	}
	seen := make(map[string]bool, len(document.ConfirmationUnits))
	for index := range document.ConfirmationUnits {
		item := &document.ConfirmationUnits[index]
		item.UnitKey = strings.TrimSpace(item.UnitKey)
		item.Title = strings.TrimSpace(item.Title)
		item.Markdown = strings.TrimSpace(item.Markdown)
		if item.UnitKey == "" || item.Title == "" || item.Markdown == "" || item.Ordinal < 0 || seen[item.UnitKey] {
			return nil, true, fmt.Errorf("%w: invalid confirmation unit", ErrInvalidPayload)
		}
		digest := sha256.Sum256([]byte(item.Markdown))
		actual := hex.EncodeToString(digest[:])
		if item.ContentHash != actual && item.ContentHash != "sha256:"+actual {
			return nil, true, fmt.Errorf("%w: confirmation unit hash mismatch", ErrInvalidPayload)
		}
		item.ContentHash = actual
		seen[item.UnitKey] = true
	}
	for _, item := range document.ConfirmationUnits {
		for _, dependency := range item.DependsOn {
			if !seen[dependency] {
				return nil, true, fmt.Errorf("%w: confirmation dependency is missing", ErrInvalidPayload)
			}
		}
	}
	return document.ConfirmationUnits, true, nil
}
