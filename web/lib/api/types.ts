import type { components } from "./schema";


type Schemas = components["schemas"];

export type DisplayStatus =
  | "NEEDS_INPUT"
  | "IN_PROGRESS"
  | "AWAITING_CONFIRMATION"
  | "COMPLETED"
  | "FAILED"
  | "STOPPED";

export type TaskSummary = Omit<
  Schemas["TaskSummaryView"],
  "display_status" | "run_status" | "current_unit_sequence"
> & {
  display_status: DisplayStatus;
  run_status: string | null;
  current_unit_sequence: number | null;
};

export type TaskList = Omit<Schemas["TaskListResponse"], "items"> & {
  items: TaskSummary[];
};

export type ActionType =
  | "SEND_MESSAGE"
  | "CONFIRM_OUTLINE"
  | "CONFIRM_UNIT"
  | "APPROVE_REVISION_PLAN"
  | "FINALIZE_PRD"
  | "REOPEN_PRD";

export type AvailableAction = Omit<
  Schemas["AvailableActionView"],
  "type" | "payload"
> & {
  type: ActionType;
  payload: Record<string, unknown>;
};

export type OutlineNode = Schemas["OutlineNodeView"];
export type Unit = Schemas["UnitView"];
export type QualityIssue = Schemas["QualityIssueView"];
export type Investigation = Schemas["InvestigationView"];

export type TaskDetail = Omit<
  Schemas["TaskDetailResponse"],
  "task" | "available_actions"
> & {
  task: TaskSummary;
  available_actions: AvailableAction[];
};

export interface ApiErrorBody {
  error_code: string;
  message: string;
  retryable: boolean;
  correlation_id: string;
}

export type ExportMode = Schemas["ExportMode"];

export type ExportPreview = Omit<
  Schemas["ExportPreviewView"],
  "mode" | "bound_title" | "bound_safe_url"
> & {
  mode: ExportMode;
  bound_title: string | null;
  bound_safe_url: string | null;
};

export type ExportStatus =
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "RESULT_UNKNOWN"
  | "MANUAL_REVIEW";

export type ExportRun = Omit<
  Schemas["ExportRunView"],
  | "mode"
  | "status"
  | "binding_id"
  | "safe_url"
  | "display_title"
  | "error_code"
  | "completed_at"
> & {
  mode: ExportMode;
  status: ExportStatus;
  binding_id: string | null;
  safe_url: string | null;
  display_title: string | null;
  error_code: string | null;
  completed_at: string | null;
};

export type ExportList = Omit<Schemas["ExportListResponse"], "items"> & {
  items: ExportRun[];
};

export type RepositoryBinding =
  Schemas["RepositoryBindingView"];

export type RepositoryBindingList = Omit<
  Schemas["RepositoryBindingListResponse"],
  "items"
> & {
  items: RepositoryBinding[];
};
