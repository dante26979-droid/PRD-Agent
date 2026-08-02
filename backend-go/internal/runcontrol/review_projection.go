package runcontrol

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"
)

type PublishReadinessView struct {
	Ready   bool     `json:"ready"`
	Reasons []string `json:"reasons"`
}

type ReviewView struct {
	WorkflowVersion  WorkflowVersion           `json:"workflow_version"`
	Task             Task                      `json:"task"`
	Outline          *ReviewOutlineVersion     `json:"outline,omitempty"`
	Units            []ConfirmationUnitVersion `json:"units"`
	FullReview       *FullReviewReport         `json:"full_review,omitempty"`
	CurrentUnitKey   string                    `json:"current_unit_key,omitempty"`
	DocumentMarkdown string                    `json:"document_markdown,omitempty"`
	PublishReadiness PublishReadinessView      `json:"publish_readiness"`
	AvailableActions []string                  `json:"available_actions"`
}

type ReviewProjection interface {
	GetReviewProjection(ctx context.Context, tenantID, ownerID, taskID string) (ReviewView, error)
	GetFullReviewReport(ctx context.Context, tenantID, ownerID, taskID string) (FullReviewReport, error)
}

type reviewProjectionSource interface {
	GetTask(context.Context, string, string, string) (Task, error)
	ListRuns(context.Context, string, string, string) ([]AgentRun, error)
	GetLatestDraft(context.Context, string, string, string) (WorkingDraft, error)
	GetReviewOutline(context.Context, string, string, string) (ReviewOutlineVersion, error)
	ListConfirmationUnits(context.Context, string, string, string) ([]ConfirmationUnitVersion, error)
	GetFullReviewReport(context.Context, string, string, string) (FullReviewReport, error)
}

func BuildReviewProjection(ctx context.Context, source reviewProjectionSource, tenantID, ownerID, taskID string) (ReviewView, error) {
	task, err := source.GetTask(ctx, tenantID, ownerID, taskID)
	if err != nil {
		return ReviewView{}, err
	}
	view := ReviewView{
		WorkflowVersion:  WorkflowVersionV1,
		Task:             task,
		Units:            []ConfirmationUnitVersion{},
		PublishReadiness: PublishReadinessView{Reasons: []string{"WORKING_DRAFT_NOT_REVIEWABLE"}},
		AvailableActions: []string{},
	}
	runs, err := source.ListRuns(ctx, tenantID, ownerID, taskID)
	if err != nil {
		return ReviewView{}, err
	}
	if len(runs) > 0 {
		view.WorkflowVersion = runs[0].WorkflowVersion
	}
	outline, outlineErr := source.GetReviewOutline(ctx, tenantID, ownerID, taskID)
	if outlineErr != nil && !errors.Is(outlineErr, ErrNotFound) {
		return ReviewView{}, outlineErr
	}
	if outlineErr == nil {
		view.WorkflowVersion = WorkflowVersionV4
		view.Outline = &outline
		units, err := source.ListConfirmationUnits(ctx, tenantID, ownerID, taskID)
		if err != nil {
			return ReviewView{}, err
		}
		view.Units = units
		for _, unit := range units {
			if unit.ConfirmationStatus == string(UnitReviewing) || unit.ConfirmationStatus == string(UnitReopened) {
				view.CurrentUnitKey = unit.UnitKey
				break
			}
		}
		report, reportErr := source.GetFullReviewReport(ctx, tenantID, ownerID, taskID)
		if reportErr != nil && !errors.Is(reportErr, ErrNotFound) {
			return ReviewView{}, reportErr
		}
		if reportErr == nil {
			view.FullReview = &report
		}
		view.DocumentMarkdown = renderReviewDocument(outline, units)
		view.PublishReadiness = v4PublishReadiness(task, outline, units, view.FullReview)
		view.AvailableActions = reviewActions(task, outline, units, view.PublishReadiness.Ready)
		return view, nil
	}
	if view.WorkflowVersion == WorkflowVersionV4 {
		view.PublishReadiness = PublishReadinessView{Reasons: []string{"OUTLINE_NOT_AVAILABLE"}}
		return view, nil
	}
	draft, draftErr := source.GetLatestDraft(ctx, tenantID, ownerID, taskID)
	if draftErr == nil {
		view.DocumentMarkdown = workingDraftMarkdown(draft.Content)
		if task.Status == "REVIEWABLE" {
			view.PublishReadiness = PublishReadinessView{Ready: true, Reasons: []string{}}
			view.AvailableActions = append(view.AvailableActions, "PUBLISH")
		}
	} else if !errors.Is(draftErr, ErrNotFound) {
		return ReviewView{}, draftErr
	}
	return view, nil
}

func v4PublishReadiness(task Task, outline ReviewOutlineVersion, units []ConfirmationUnitVersion, report *FullReviewReport) PublishReadinessView {
	reasons := make([]string, 0)
	if outline.Status != OutlineLocked {
		reasons = append(reasons, "OUTLINE_NOT_LOCKED")
	}
	for _, unit := range units {
		if unit.ConfirmationStatus != string(UnitConfirmed) {
			reasons = append(reasons, "UNCONFIRMED_UNIT")
			break
		}
	}
	if report == nil || report.Disposition != "PASSED" {
		reasons = append(reasons, "FULL_REVIEW_NOT_PASSED")
	}
	if task.Status != string(ReviewReviewable) {
		reasons = append(reasons, "TASK_NOT_REVIEWABLE")
	}
	return PublishReadinessView{Ready: len(reasons) == 0, Reasons: reasons}
}

func reviewActions(task Task, outline ReviewOutlineVersion, units []ConfirmationUnitVersion, publishReady bool) []string {
	actions := make([]string, 0)
	if outline.Status == OutlineDraft && task.Status == string(ReviewOutlineReview) {
		actions = append(actions, "CONFIRM_OUTLINE")
	}
	for _, unit := range units {
		if unit.ConfirmationStatus == string(UnitReviewing) {
			actions = append(actions, "CONFIRM_UNIT")
		}
		if unit.ConfirmationStatus == string(UnitConfirmed) {
			actions = append(actions, "REOPEN_UNIT")
		}
	}
	if publishReady {
		actions = append(actions, "PUBLISH")
	}
	sort.Strings(actions)
	return uniqueStrings(actions, false)
}

func renderReviewDocument(outline ReviewOutlineVersion, units []ConfirmationUnitVersion) string {
	sort.Slice(units, func(i, j int) bool { return units[i].Ordinal < units[j].Ordinal })
	parts := []string{"# " + strings.TrimSpace(outline.Candidate.Title)}
	for _, unit := range units {
		if strings.TrimSpace(unit.Markdown) != "" {
			parts = append(parts, strings.TrimSpace(unit.Markdown))
		}
	}
	return strings.Join(parts, "\n\n")
}

func workingDraftMarkdown(content []byte) string {
	var value struct {
		Markdown string `json:"markdown"`
	}
	if json.Unmarshal(content, &value) == nil && value.Markdown != "" {
		return value.Markdown
	}
	return string(content)
}

func (s *MemoryStore) GetFullReviewReport(_ context.Context, tenantID, ownerID, taskID string) (FullReviewReport, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	task, ok := s.tasks[taskID]
	if !ok || task.TenantID != tenantID || task.OwnerID != ownerID {
		return FullReviewReport{}, ErrNotFound
	}
	report, ok := s.fullReviewReports[taskID]
	if !ok {
		return FullReviewReport{}, ErrNotFound
	}
	report.UnitHashes = cloneStringMap(report.UnitHashes)
	report.Payload = append([]byte(nil), report.Payload...)
	return report, nil
}

func (s *MemoryStore) GetReviewProjection(ctx context.Context, tenantID, ownerID, taskID string) (ReviewView, error) {
	return BuildReviewProjection(ctx, s, tenantID, ownerID, taskID)
}

var _ ReviewProjection = (*MemoryStore)(nil)
