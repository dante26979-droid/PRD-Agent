export interface ControlTask {
  task_id: string;
  tenant_id: string;
  owner_id: string;
  message: string;
  status: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface AgentRun {
  run_id: string;
  task_id: string;
  status: string;
  queue_slot_acquired: boolean;
  attempt_count: number;
  created_at: string;
  updated_at: string;
}

export interface TaskWithRun {
  task: ControlTask;
  run: AgentRun;
}

export interface DraftView {
  draft: {
    draft_id: string;
    run_id: string;
    content_hash: string;
    task_version: number;
    created_at: string;
  };
  content: { markdown?: string } | string;
}

export interface EvidenceView {
  evidence_id: string;
  run_id: string;
  source_type: string;
  source_id: string;
  locator: string;
  excerpt_hash: string;
}

export interface AttemptView {
  attempt_id: string;
  run_id: string;
  operation: string;
  provider?: string;
  status: string;
  error_category?: string;
}

export interface PublishPreview {
  publish_id: string;
  confirmation_token: string;
  task_version: number;
  draft_version: number;
  content_hash: string;
  target: string;
  expires_at: string;
}

export interface PublishView {
  publish_id: string;
  task_id: string;
  status: string;
  safe_url?: string;
  provider_revision?: string;
  error_code?: string;
  retryable: boolean;
  updated_at: string;
}

export interface ReviewOutline {
  outline_id: string;
  outline_version_id: string;
  status: "DRAFT" | "LOCKED";
  content_hash: string;
  candidate: {
    title: string;
    requirement_size: string;
    units: Array<{ unit_key: string; title: string; ordinal: number; depends_on: string[] }>;
  };
}

export interface ReviewUnit {
  unit_id: string;
  unit_version_id: string;
  unit_key: string;
  title: string;
  order: number;
  markdown: string;
  content_hash: string;
  confirmation_status: "PENDING" | "REVIEWING" | "CONFIRMED" | "REOPENED";
  depends_on: string[];
}

export interface FullReviewView {
  report_id: string;
  disposition: "PASSED" | "NEEDS_REVISION";
  content_hash: string;
  payload?: {
    issues?: Array<{ code?: string; message?: string; affected_unit_keys?: string[] }>;
  };
}

export interface ReviewView {
  workflow_version: "agent-runtime.v1" | "agent-runtime.v4";
  task: ControlTask;
  outline?: ReviewOutline;
  units: ReviewUnit[];
  full_review?: FullReviewView;
  current_unit_key?: string;
  document_markdown?: string;
  publish_readiness: { ready: boolean; reasons: string[] };
  available_actions: Array<"CONFIRM_OUTLINE" | "CONFIRM_UNIT" | "REOPEN_UNIT" | "PUBLISH">;
}
