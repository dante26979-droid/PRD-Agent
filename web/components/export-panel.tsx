"use client";

import { useEffect, useMemo, useState } from "react";

import {
  ApiError,
  executeFeishuExport,
  listExports,
  previewFeishuExport,
} from "@/lib/api/client";
import type {
  ExportMode,
  ExportPreview,
  ExportRun,
} from "@/lib/api/types";


function safeDocumentUrl(value: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    const allowed =
      url.protocol === "https:" &&
      (url.hostname.endsWith(".feishu.cn") ||
        url.hostname.endsWith(".larksuite.com"));
    return allowed ? url.toString() : undefined;
  } catch {
    return undefined;
  }
}

export function ExportPanel({
  taskId,
  taskVersion,
}: {
  taskId: string;
  taskVersion: number;
}) {
  const [runs, setRuns] = useState<ExportRun[]>([]);
  const [preview, setPreview] = useState<ExportPreview | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void listExports(taskId)
      .then((value) => {
        if (active) setRuns(value.items);
      })
      .catch(() => {
        if (active) setError("导出记录加载失败。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [taskId]);

  const latestSuccess = useMemo(
    () => runs.find((item) => item.status === "SUCCEEDED" && item.binding_id),
    [runs],
  );
  const mode: ExportMode = latestSuccess ? "OVERWRITE_BOUND" : "CREATE";

  async function loadPreview() {
    setBusy(true);
    setError("");
    try {
      setPreview(await previewFeishuExport(taskId, mode, taskVersion));
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : "无法生成飞书导出预览。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      const run = await executeFeishuExport(taskId, preview, taskVersion);
      setRuns((current) => [run, ...current]);
      setPreview(null);
      if (run.status !== "SUCCEEDED") {
        setError(
          run.status === "MANUAL_REVIEW"
            ? "飞书返回结果不明确，请人工核对，系统不会自动重复创建。"
            : "飞书导出未完成，请根据状态重试。",
        );
      }
    } catch (reason) {
      setError(
        reason instanceof ApiError ? reason.message : "飞书导出失败。",
      );
    } finally {
      setBusy(false);
    }
  }

  const safeUrl = safeDocumentUrl(latestSuccess?.safe_url ?? null);

  return (
    <section className="export-panel" aria-label="飞书导出">
      <div>
        <span>EXTERNAL DOCUMENT</span>
        <strong>{latestSuccess ? "已绑定飞书文档" : "尚未导出"}</strong>
      </div>
      {safeUrl && (
        <a href={safeUrl} target="_blank" rel="noopener noreferrer">
          打开飞书文档
        </a>
      )}
      <button
        className="secondary-button"
        disabled={loading || busy}
        onClick={() => void loadPreview()}
      >
        {busy
          ? "处理中…"
          : latestSuccess
            ? "更新飞书文档"
            : "导出到飞书"}
      </button>
      {error && <p role="alert">{error}</p>}

      {preview && (
        <div className="export-confirmation" role="dialog" aria-modal="true">
          <span>EXPORT PREVIEW · DOC V{preview.document_version}</span>
          <h3>{preview.title}</h3>
          <p>
            {preview.mode === "OVERWRITE_BOUND"
              ? "当前版本将整份覆盖任务已绑定的飞书文档。"
              : "将创建一份新的飞书文档并绑定到当前任务。"}
          </p>
          {preview.unresolved_items.length > 0 && (
            <div>
              <strong>未解决事项</strong>
              <ul>
                {preview.unresolved_items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          )}
          <div className="export-confirmation-actions">
            <button
              className="secondary-button"
              disabled={busy}
              onClick={() => setPreview(null)}
            >
              取消
            </button>
            <button
              className="primary-button"
              disabled={busy}
              onClick={() => void confirm()}
            >
              {preview.mode === "OVERWRITE_BOUND" ? "确认覆盖" : "确认创建"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
