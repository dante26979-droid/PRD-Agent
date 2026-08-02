import type {
  ApiErrorBody,
  ExportList,
  ExportMode,
  ExportPreview,
  ExportRun,
  TaskDetail,
  TaskList,
} from "@/lib/api/types";
import type {
  AgentRun,
  AttemptView,
  ControlTask,
  DraftView,
  EvidenceView,
  PublishPreview as AgentPublishPreview,
  PublishView,
  ReviewView,
  TaskWithRun,
} from "@/lib/api/agent-types";

const base = "/backend/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body?: ApiErrorBody,
  ) {
    super(message);
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    let body: ApiErrorBody | undefined;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = undefined;
    }
    throw new ApiError(body?.message ?? "请求失败，请稍后重试。", response.status, body);
  }
  return (await response.json()) as T;
}

function command<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  });
}

export function listTasks(cursor?: string): Promise<TaskList> {
  const suffix = cursor ? `?cursor=${encodeURIComponent(cursor)}` : "";
  return request<TaskList>(`/tasks${suffix}`);
}

export function getTask(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`);
}

export function startTask(message: string): Promise<TaskDetail> {
  return command<TaskDetail>("/tasks/from-message", { message });
}

export function reply(
  taskId: string,
  message: string,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}/messages`, {
    message,
    expected_task_version: expectedTaskVersion,
  });
}

export function confirmOutline(
  taskId: string,
  outlineVersion: number,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(
    `/tasks/${encodeURIComponent(taskId)}/outline/confirm`,
    {
      outline_version: outlineVersion,
      expected_task_version: expectedTaskVersion,
    },
  );
}

export function confirmUnit(
  taskId: string,
  unitId: string,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(
    `/tasks/${encodeURIComponent(taskId)}/units/${encodeURIComponent(unitId)}/confirm`,
    { expected_task_version: expectedTaskVersion },
  );
}

export function approveRevision(
  taskId: string,
  payload: Record<string, unknown>,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(
    `/tasks/${encodeURIComponent(taskId)}/revisions/approve`,
    { ...payload, expected_task_version: expectedTaskVersion },
  );
}

export function finalizePrd(
  taskId: string,
  payload: Record<string, unknown>,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}/finalize`, {
    ...payload,
    expected_task_version: expectedTaskVersion,
  });
}

export function reopenPrd(
  taskId: string,
  unitIds: string[],
  reason: string,
  expectedTaskVersion: number,
): Promise<TaskDetail> {
  return command<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}/reopen`, {
    unit_ids: unitIds,
    reason,
    expected_task_version: expectedTaskVersion,
  });
}

export function eventUrl(taskId: string, sequence: number): string {
  return `${base}/tasks/${encodeURIComponent(taskId)}/events?after_sequence=${sequence}`;
}

export function listAgentTasks(): Promise<{ items: ControlTask[] }> {
  return request<{ items: ControlTask[] }>("/tasks?limit=50");
}

export function startAgentTask(message: string): Promise<TaskWithRun> {
  return command<TaskWithRun>("/tasks/from-message", { message });
}

export function getAgentTask(taskId: string): Promise<{ task: ControlTask }> {
  return request<{ task: ControlTask }>(`/tasks/${encodeURIComponent(taskId)}`);
}

export function listAgentRuns(taskId: string): Promise<{ items: AgentRun[] }> {
  return request<{ items: AgentRun[] }>(`/tasks/${encodeURIComponent(taskId)}/runs`);
}

export function getAgentDraft(taskId: string): Promise<DraftView> {
  return request<DraftView>(`/tasks/${encodeURIComponent(taskId)}/draft`);
}

export function getAgentReview(taskId: string): Promise<ReviewView> {
  return request<ReviewView>(`/tasks/${encodeURIComponent(taskId)}/review`);
}

export function confirmAgentOutline(
  taskId: string,
  outlineVersionId: string,
  expectedTaskVersion: number,
): Promise<unknown> {
  return command(`/tasks/${encodeURIComponent(taskId)}/outline/confirm`, {
    outline_version_id: outlineVersionId,
    expected_task_version: expectedTaskVersion,
  });
}

export function confirmAgentUnit(
  taskId: string,
  unitVersionId: string,
  expectedTaskVersion: number,
): Promise<unknown> {
  return command(
    `/tasks/${encodeURIComponent(taskId)}/confirmation-units/${encodeURIComponent(unitVersionId)}/confirm`,
    { expected_task_version: expectedTaskVersion },
  );
}

export function reopenAgentUnit(
  taskId: string,
  unitVersionId: string,
  feedback: string,
  expectedTaskVersion: number,
): Promise<unknown> {
  return command(
    `/tasks/${encodeURIComponent(taskId)}/confirmation-units/${encodeURIComponent(unitVersionId)}/reopen`,
    { feedback, expected_task_version: expectedTaskVersion },
  );
}

export function listAgentEvidence(taskId: string): Promise<{ items: EvidenceView[] }> {
  return request<{ items: EvidenceView[] }>(`/tasks/${encodeURIComponent(taskId)}/evidence`);
}

export function listAgentAttempts(taskId: string): Promise<{ items: AttemptView[] }> {
  return request<{ items: AttemptView[] }>(`/tasks/${encodeURIComponent(taskId)}/attempts`);
}

export function stopAgentRun(taskId: string, runId: string): Promise<{ run: AgentRun }> {
  return command<{ run: AgentRun }>(
    `/tasks/${encodeURIComponent(taskId)}/runs/${encodeURIComponent(runId)}/stop`,
    {},
  );
}

export function retryAgentTask(taskId: string, taskVersion: number): Promise<{ run: AgentRun }> {
  return command<{ run: AgentRun }>(`/tasks/${encodeURIComponent(taskId)}/retry`, {
    expected_task_version: taskVersion,
  });
}

export function previewAgentPublish(
  taskId: string,
  taskVersion: number,
): Promise<{ preview: AgentPublishPreview }> {
  return command<{ preview: AgentPublishPreview }>(
    `/tasks/${encodeURIComponent(taskId)}/publish/feishu/preview`,
    { expected_task_version: taskVersion },
  );
}

export function confirmAgentPublish(
  taskId: string,
  preview: AgentPublishPreview,
): Promise<{ publish: PublishView }> {
  return command<{ publish: PublishView }>(
    `/tasks/${encodeURIComponent(taskId)}/publish/feishu`,
    {
      publish_id: preview.publish_id,
      confirmation_token: preview.confirmation_token,
      expected_task_version: preview.task_version,
    },
  );
}

export function listAgentPublishes(taskId: string): Promise<{ items: PublishView[] }> {
  return request<{ items: PublishView[] }>(`/tasks/${encodeURIComponent(taskId)}/publishes`);
}

export function agentEventUrl(taskId: string): string {
  return `${base}/tasks/${encodeURIComponent(taskId)}/events`;
}

export function listExports(taskId: string): Promise<ExportList> {
  return request<ExportList>(`/tasks/${encodeURIComponent(taskId)}/exports`);
}

export function previewFeishuExport(
  taskId: string,
  mode: ExportMode,
  expectedTaskVersion: number,
): Promise<ExportPreview> {
  return command<ExportPreview>(
    `/tasks/${encodeURIComponent(taskId)}/exports/feishu/preview`,
    {
      mode,
      expected_task_version: expectedTaskVersion,
    },
  );
}

export function executeFeishuExport(
  taskId: string,
  preview: ExportPreview,
  expectedTaskVersion: number,
): Promise<ExportRun> {
  return command<ExportRun>(
    `/tasks/${encodeURIComponent(taskId)}/exports/feishu`,
    {
      intent_id: preview.intent_id,
      confirmation_token: preview.confirmation_token,
      mode: preview.mode,
      expected_task_version: expectedTaskVersion,
    },
  );
}
