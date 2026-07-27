import type {
  ApiErrorBody,
  ExportList,
  ExportMode,
  ExportPreview,
  ExportRun,
  TaskDetail,
  TaskList,
} from "@/lib/api/types";

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
