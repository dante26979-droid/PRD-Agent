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
