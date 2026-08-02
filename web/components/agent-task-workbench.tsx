"use client";

import { useCallback, useEffect, useState } from "react";

import {
  agentEventUrl,
  ApiError,
  confirmAgentPublish,
  confirmAgentOutline,
  confirmAgentUnit,
  getAgentDraft,
  getAgentTask,
  getAgentReview,
  listAgentAttempts,
  listAgentEvidence,
  listAgentPublishes,
  listAgentRuns,
  previewAgentPublish,
  retryAgentTask,
  reopenAgentUnit,
  stopAgentRun,
} from "@/lib/api/client";
import type {
  AgentRun,
  AttemptView,
  ControlTask,
  DraftView,
  EvidenceView,
  PublishView,
  ReviewView,
} from "@/lib/api/agent-types";

import { MarkdownView } from "./markdown-view";

const terminal = new Set(["SUCCEEDED", "FAILED", "STOPPED"]);

export function AgentTaskWorkbench({ taskId }: { taskId: string }) {
  const [task, setTask] = useState<ControlTask | null>(null);
  const [runs, setRuns] = useState<AgentRun[]>([]);
  const [draft, setDraft] = useState<DraftView | null>(null);
  const [evidence, setEvidence] = useState<EvidenceView[]>([]);
  const [attempts, setAttempts] = useState<AttemptView[]>([]);
  const [publishes, setPublishes] = useState<PublishView[]>([]);
  const [review, setReview] = useState<ReviewView | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [connection, setConnection] = useState("连接中");

  const refresh = useCallback(async () => {
    const [taskValue, runValue, evidenceValue, attemptValue, publishValue, reviewValue] =
      await Promise.all([
        getAgentTask(taskId),
        listAgentRuns(taskId),
        listAgentEvidence(taskId),
        listAgentAttempts(taskId),
        listAgentPublishes(taskId),
        getAgentReview(taskId),
      ]);
    setTask(taskValue.task);
    setRuns(runValue.items);
    setEvidence(evidenceValue.items);
    setAttempts(attemptValue.items);
    setPublishes(publishValue.items);
    setReview(reviewValue);
    try {
      setDraft(await getAgentDraft(taskId));
    } catch (reason) {
      if (!(reason instanceof ApiError) || reason.status !== 404) throw reason;
      setDraft(null);
    }
    setError("");
  }, [taskId]);

  useEffect(() => {
    let active = true;
    void refresh().catch((reason) => {
      if (active) setError(reason instanceof ApiError ? reason.message : "任务加载失败。");
    });
    const poll = window.setInterval(() => void refresh(), 3_000);
    const source = new EventSource(agentEventUrl(taskId));
    const update = () => {
      setConnection("实时");
      void refresh();
      window.dispatchEvent(new Event("prd-agent:tasks-changed"));
    };
    [
      "agent.MODEL_ATTEMPT",
      "agent.EVIDENCE_APPENDED",
      "agent.CHECKPOINT_SAVED",
      "agent.DRAFT_SUBMITTED",
      "agent.RUN_COMPLETED",
      "agent.RUN_FAILED",
      "run.retry_created",
      "publish.pending",
      "publish.succeeded",
      "publish.reconciling",
      "publish.manual_review",
    ].forEach((name) => source.addEventListener(name, update));
    source.onopen = () => setConnection("实时");
    source.onerror = () => setConnection("轮询兜底");
    return () => {
      active = false;
      window.clearInterval(poll);
      source.close();
    };
  }, [refresh, taskId]);

  const latestRun = runs[0];
  const markdown =
    review?.document_markdown || (typeof draft?.content === "string"
      ? draft.content
      : draft?.content.markdown ?? "");
  const latestPublish = publishes[0];
  const canRetry = latestRun && terminal.has(latestRun.status);

  async function runAction(name: string, action: () => Promise<unknown>) {
    setBusy(name);
    setError("");
    try {
      await action();
      await refresh();
      window.dispatchEvent(new Event("prd-agent:tasks-changed"));
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : "操作失败，请稍后重试。");
    } finally {
      setBusy("");
    }
  }

  async function publish() {
    if (!task) return;
    await runAction("publish", async () => {
      const { preview } = await previewAgentPublish(taskId, task.version);
      const accepted = window.confirm(
        `确认把 Draft v${preview.draft_version} 覆盖写入固定飞书 Wiki 文档？`,
      );
      if (!accepted) return;
      await confirmAgentPublish(taskId, preview);
    });
  }

  async function confirmOutline() {
    if (!task || !review?.outline) return;
    await runAction("confirm-outline", () =>
      confirmAgentOutline(taskId, review.outline!.outline_version_id, task.version),
    );
  }

  async function confirmUnit(unitVersionId: string) {
    if (!task) return;
    await runAction("confirm-unit", () =>
      confirmAgentUnit(taskId, unitVersionId, task.version),
    );
  }

  async function reopenUnit(unitVersionId: string) {
    if (!task) return;
    const feedback = window.prompt("请说明需要修改的内容：")?.trim();
    if (!feedback) return;
    await runAction("reopen-unit", () =>
      reopenAgentUnit(taskId, unitVersionId, feedback, task.version),
    );
  }

  if (!task && !error) return <div className="skeleton-workbench">正在加载 Agent 状态…</div>;
  if (!task) return <div className="center-error"><h1>无法打开任务</h1><p>{error}</p></div>;

  return (
    <div className="workbench">
      <header className="task-header">
        <div>
          <div className="eyebrow">AGENT TASK / {task.task_id.slice(-8)}</div>
          <h1>{task.message}</h1>
        </div>
        <div className="header-meta">
          <span className="connection"><i />{connection}</span>
          <span className="version-tag">v{task.version}</span>
          <span className="state-pill">{latestRun?.status ?? "准备中"}</span>
        </div>
      </header>
      {error && <div className="global-error" role="alert">{error}</div>}
      <div className="workbench-grid">
        <section className="workflow-column">
          <div className="section-title"><span>01</span><h2>Agent 执行</h2></div>
          <div className="workflow-card">
            <div className="card-topline"><span>RUN HISTORY</span><i>{latestRun?.status}</i></div>
            <h3>执行与恢复</h3>
            {runs.map((run) => (
              <p key={run.run_id}>
                {run.run_id.slice(-10)} · {run.status} · attempt {run.attempt_count}
              </p>
            ))}
            <div className="export-confirmation-actions">
              {latestRun && !terminal.has(latestRun.status) && (
                <button className="secondary-button" disabled={!!busy} onClick={() =>
                  void runAction("stop", () => stopAgentRun(taskId, latestRun.run_id))
                }>停止</button>
              )}
              {canRetry && (
                <button className="primary-button" disabled={!!busy} onClick={() =>
                  void runAction("retry", () => retryAgentTask(taskId, task.version))
                }>重新执行</button>
              )}
            </div>
          </div>
          {review?.workflow_version === "agent-runtime.v4" && (
            <div className="workflow-card" data-testid="review-workflow">
              <div className="card-topline"><span>REVIEW WORKFLOW</span><i>{task.status}</i></div>
              <h3>{review.outline?.candidate.title ?? "正在生成大纲"}</h3>
              {review.available_actions.includes("CONFIRM_OUTLINE") && review.outline && (
                <button className="primary-button" disabled={!!busy} onClick={() => void confirmOutline()}>
                  确认大纲
                </button>
              )}
              {review.units.map((unit) => (
                <div key={unit.unit_id} className="review-unit-row">
                  <p><strong>{unit.title}</strong> · {unit.confirmation_status}</p>
                  {unit.confirmation_status === "REVIEWING" && (
                    <button className="primary-button" disabled={!!busy} onClick={() => void confirmUnit(unit.unit_version_id)}>
                      确认单元
                    </button>
                  )}
                  {unit.confirmation_status === "CONFIRMED" && (
                    <button className="secondary-button" disabled={!!busy} onClick={() => void reopenUnit(unit.unit_version_id)}>
                      重新打开
                    </button>
                  )}
                </div>
              ))}
              {review.full_review && (
                <p>完整审阅：{review.full_review.disposition}</p>
              )}
              {!review.publish_readiness.ready && review.publish_readiness.reasons.length > 0 && (
                <p>发布等待：{review.publish_readiness.reasons.join("、")}</p>
              )}
            </div>
          )}
          <div className="workflow-card">
            <div className="card-topline"><span>MODEL ATTEMPTS</span><i>{attempts.length}</i></div>
            {attempts.length === 0 ? <p>等待模型调用。</p> : attempts.map((attempt) => (
              <p key={attempt.attempt_id}>
                {attempt.operation} · {attempt.provider ?? "local"} · {attempt.status}
                {attempt.error_category ? ` · ${attempt.error_category}` : ""}
              </p>
            ))}
          </div>
          <div className="workflow-card">
            <div className="card-topline"><span>EVIDENCE</span><i>{evidence.length}</i></div>
            {evidence.length === 0 ? <p>当前 Draft 仅基于用户输入。</p> : evidence.map((item) => (
              <p key={item.evidence_id}>{item.source_type} · {item.locator}</p>
            ))}
          </div>
        </section>
        <aside className="inspector-column">
          <div className="section-title"><span>02</span><h2>交付状态</h2></div>
          <div className="progress-card">
            <div className="progress-head"><span>飞书固定 Wiki</span><strong>{latestPublish?.status ?? "未发布"}</strong></div>
            {latestPublish?.error_code && <p>{latestPublish.error_code}</p>}
            {safeFeishuURL(latestPublish?.safe_url) && (
              <a href={safeFeishuURL(latestPublish?.safe_url)} target="_blank" rel="noreferrer">打开文档</a>
            )}
            <button className="secondary-button" disabled={!review?.publish_readiness.ready || !!busy} onClick={() => void publish()}>
              {busy === "publish" ? "提交中…" : "发布到飞书"}
            </button>
          </div>
        </aside>
        <section className="document-column">
          <div className="section-title"><span>03</span><h2>{review?.workflow_version === "agent-runtime.v4" ? "Review Document" : "Working Draft"}</h2></div>
          {markdown ? (
            <article className="document-paper">
              <MarkdownView content={markdown} />
              <div className="document-hash"><span>CONTENT HASH</span><code>{review?.full_review?.content_hash ?? draft?.draft.content_hash}</code></div>
            </article>
          ) : (
            <div className="document-empty"><div className="document-empty-mark">¶</div><h2>Agent 正在生成 Draft</h2><p>页面会自动恢复连接并刷新已持久化进度。</p></div>
          )}
        </section>
      </div>
    </div>
  );
}

function safeFeishuURL(value?: string): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol === "https:" &&
      (parsed.hostname.endsWith(".feishu.cn") || parsed.hostname.endsWith(".larksuite.com"))
    ) return parsed.toString();
  } catch {
    return undefined;
  }
  return undefined;
}
