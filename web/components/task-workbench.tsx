"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  approveRevision,
  confirmOutline,
  confirmUnit,
  eventUrl,
  finalizePrd,
  getTask,
  reopenPrd,
  reply,
} from "@/lib/api/client";
import type {
  AvailableAction,
  Investigation,
  TaskDetail,
} from "@/lib/api/types";

import { MarkdownView } from "./markdown-view";
import { ExportPanel } from "./export-panel";

const publicEvents = [
  "task.started",
  "task.input_required",
  "outline.ready_for_confirmation",
  "outline.confirmed",
  "unit.ready_for_confirmation",
  "grounding.blocked",
  "unit.confirmed",
  "quality.checked",
  "revision.approved",
  "document.updated",
  "document.finalized",
  "document.reopened",
  "run.failed",
];

const displayLabel: Record<string, string> = {
  NEEDS_INPUT: "等待补充",
  IN_PROGRESS: "Agent 处理中",
  AWAITING_CONFIRMATION: "等待确认",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  STOPPED: "已停止",
};

export function TaskWorkbench({ taskId }: { taskId: string }) {
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState<"live" | "reconnecting" | "polling">("live");
  const [busyAction, setBusyAction] = useState("");
  const [replyText, setReplyText] = useState("");
  const latestSequence = useRef(0);

  const refresh = useCallback(async () => {
    const value = await getTask(taskId);
    latestSequence.current = value.latest_event_sequence;
    setDetail(value);
    setError("");
    return value;
  }, [taskId]);

  useEffect(() => {
    let active = true;
    let source: EventSource | undefined;
    let polling: ReturnType<typeof setInterval> | undefined;
    let failures = 0;

    async function connect() {
      try {
        const value = await refresh();
        if (!active) return;
        setLoading(false);
        source = new EventSource(eventUrl(taskId, value.latest_event_sequence));
        const update = () => {
          failures = 0;
          setConnection("live");
          void refresh().then(() => {
            window.dispatchEvent(new Event("prd-agent:tasks-changed"));
          });
        };
        publicEvents.forEach((name) => source?.addEventListener(name, update));
        source.onopen = () => setConnection("live");
        source.onerror = () => {
          failures += 1;
          setConnection("reconnecting");
          if (failures >= 3) {
            source?.close();
            setConnection("polling");
            polling = setInterval(() => void refresh(), 3_000);
          }
        };
      } catch (reason) {
        if (!active) return;
        setLoading(false);
        setError(
          reason instanceof ApiError
            ? reason.message
            : "任务加载失败，请检查 API 连接。",
        );
      }
    }

    void connect();
    return () => {
      active = false;
      source?.close();
      if (polling) clearInterval(polling);
    };
  }, [refresh, taskId]);

  async function execute(
    action: AvailableAction,
    custom?: { message?: string; reason?: string },
  ) {
    if (!detail || busyAction) return;
    setBusyAction(action.type);
    setError("");
    try {
      let next: TaskDetail;
      switch (action.type) {
        case "SEND_MESSAGE":
          next = await reply(
            taskId,
            custom?.message ?? replyText,
            action.expected_task_version,
          );
          setReplyText("");
          break;
        case "CONFIRM_OUTLINE":
          next = await confirmOutline(
            taskId,
            Number(action.payload.outline_version),
            action.expected_task_version,
          );
          break;
        case "CONFIRM_UNIT":
          next = await confirmUnit(
            taskId,
            action.target_id!,
            action.expected_task_version,
          );
          break;
        case "APPROVE_REVISION_PLAN":
          next = await approveRevision(
            taskId,
            action.payload,
            action.expected_task_version,
          );
          break;
        case "FINALIZE_PRD":
          next = await finalizePrd(
            taskId,
            action.payload,
            action.expected_task_version,
          );
          break;
        case "REOPEN_PRD":
          next = await reopenPrd(
            taskId,
            action.payload.unit_ids as string[],
            custom?.reason ?? "需求发生变化，需要重新确认",
            action.expected_task_version,
          );
          break;
      }
      setDetail(next);
      latestSequence.current = next.latest_event_sequence;
      window.dispatchEvent(new Event("prd-agent:tasks-changed"));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        await refresh();
        setError("任务已被更新，页面已刷新。请核对最新内容后再次操作。");
      } else {
        setError(
          reason instanceof ApiError
            ? `${reason.message}（${reason.body?.correlation_id ?? "no-id"}）`
            : "操作失败，请稍后重试。",
        );
      }
    } finally {
      setBusyAction("");
    }
  }

  async function submitReply(event: FormEvent) {
    event.preventDefault();
    const action = detail?.available_actions.find(
      (item) => item.type === "SEND_MESSAGE",
    );
    if (action && replyText.trim()) {
      await execute(action, { message: replyText.trim() });
    }
  }

  if (loading) return <WorkbenchSkeleton />;
  if (!detail) {
    return (
      <div className="center-error">
        <div className="eyebrow">TASK UNAVAILABLE</div>
        <h1>无法打开这项任务</h1>
        <p>{error || "任务不存在或当前身份没有访问权限。"}</p>
        <button className="secondary-button" onClick={() => location.reload()}>
          重新加载
        </button>
      </div>
    );
  }

  const sendAction = detail.available_actions.find(
    (item) => item.type === "SEND_MESSAGE",
  );
  const commandActions = detail.available_actions.filter(
    (item) => item.type !== "SEND_MESSAGE",
  );
  const currentUnit = detail.outline?.units.find(
    (item) => item.sequence === detail.task.current_unit_sequence,
  );

  return (
    <div className="workbench">
      <header className="task-header">
        <div>
          <div className="eyebrow">TASK / {detail.task.task_id.slice(-8)}</div>
          <h1>{detail.task.title}</h1>
        </div>
        <div className="header-meta">
          <span className={`connection connection-${connection}`}>
            <i />
            {connection === "live"
              ? "实时"
              : connection === "polling"
                ? "轮询"
                : "重连中"}
          </span>
          <span className="version-tag">v{detail.task.version}</span>
          <span className={`state-pill status-${detail.task.display_status}`}>
            {displayLabel[detail.task.display_status] ?? detail.task.display_status}
          </span>
        </div>
      </header>

      {error && <div className="global-error" role="alert">{error}</div>}

      <div className="workbench-grid">
        <section className="workflow-column" aria-label="工作流">
          <SectionTitle index="01" title="对话与确认" />
          <div className="timeline">
            {detail.messages.map((message) => (
              <div className="timeline-entry user-entry" key={message.message_id}>
                <div className="timeline-avatar">你</div>
                <div>
                  <span className="timeline-label">需求输入</span>
                  <p>{message.content}</p>
                </div>
              </div>
            ))}

            {detail.brief && (
              <WorkflowCard
                eyebrow="REQUIREMENT BRIEF"
                title="需求已结构化"
                status={detail.task.task_status}
              >
                <Brief brief={detail.brief} />
              </WorkflowCard>
            )}

            {detail.outline && (
              <WorkflowCard
                eyebrow={`OUTLINE · VERSION ${detail.outline.version}`}
                title={detail.outline.title}
                status={detail.outline.status}
              >
                <ol className="outline-list">
                  {detail.outline.nodes.map((node) => (
                    <li
                      key={node.node_id}
                      style={{ marginLeft: `${(node.level - 1) * 18}px` }}
                    >
                      <span>{String(node.sequence).padStart(2, "0")}</span>
                      <div>
                        <strong>{node.title}</strong>
                        <p>{node.purpose}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              </WorkflowCard>
            )}

            {currentUnit?.content && (
              <WorkflowCard
                eyebrow={`CONFIRMATION UNIT · ${currentUnit.sequence}`}
                title={currentUnit.title}
                status={currentUnit.status}
                accent
              >
                <MarkdownView content={currentUnit.content} />
                {detail.grounding
                  .filter((item) => item.unit_id === currentUnit.unit_id)
                  .map((grounding) => (
                    <div
                      className={`gate-summary ${grounding.confirmable ? "passed" : "blocked"}`}
                      key={grounding.grounding_run_id}
                    >
                      <span>来源校验 · {grounding.status}</span>
                      <strong>
                        {grounding.confirmable ? "可确认" : "需要处理"}
                      </strong>
                    </div>
                  ))}
              </WorkflowCard>
            )}
          </div>

          {sendAction && (
            <form className="inline-composer" onSubmit={submitReply}>
              <textarea
                value={replyText}
                onChange={(event) => setReplyText(event.target.value)}
                placeholder="补充信息或说明希望调整的内容…"
                rows={3}
              />
              <button
                className="primary-button"
                disabled={!replyText.trim() || Boolean(busyAction)}
              >
                {busyAction === "SEND_MESSAGE" ? "发送中…" : "发送补充"}
              </button>
            </form>
          )}

          {commandActions.length > 0 && (
            <div className="action-dock">
              <div>
                <small>NEXT DECISION</small>
                <strong>内容不会在你确认前继续推进</strong>
              </div>
              {commandActions.map((action) => (
                <button
                  className={action.destructive ? "secondary-button" : "primary-button"}
                  disabled={Boolean(busyAction)}
                  key={action.type}
                  onClick={() => void execute(action)}
                >
                  {busyAction === action.type ? "处理中…" : `${action.label} →`}
                </button>
              ))}
            </div>
          )}
        </section>

        <aside className="inspector-column">
          <SectionTitle index="02" title="执行轨迹" />
          <div className="progress-card">
            <div className="progress-head">
              <span>Workflow</span>
              <strong>{detail.run.status}</strong>
            </div>
            <UnitRail
              currentSequence={detail.task.current_unit_sequence}
              units={detail.outline?.units ?? []}
            />
          </div>

          {detail.investigations.map((investigation) => (
            <InvestigationCard
              investigation={investigation}
              key={investigation.investigation_id}
            />
          ))}

          {detail.quality.flatMap((item) => item.issues).length > 0 && (
            <div className="issue-panel">
              <div className="panel-heading">
                <span>QUALITY ISSUES</span>
                <strong>
                  {detail.quality.flatMap((item) => item.issues).length}
                </strong>
              </div>
              {detail.quality.flatMap((item) => item.issues).map((issue) => (
                <div className="issue-item" key={issue.issue_id}>
                  <span className={`severity severity-${issue.severity}`}>
                    {issue.severity}
                  </span>
                  <strong>{issue.description}</strong>
                  <p>{issue.suggested_resolution}</p>
                </div>
              ))}
            </div>
          )}

          <div className="trace-note">
            <span>可追溯原则</span>
            <p>
              界面只展示公开事件和校验摘要。模型私有推理、凭证与完整工具参数不会进入浏览器。
            </p>
          </div>
        </aside>

        <section className="document-column" aria-label="PRD 文档">
          <div className="document-heading">
            <SectionTitle index="03" title="PRD 文档" />
            {detail.document && (
              <span className="version-tag">doc v{detail.document.version}</span>
            )}
          </div>
          {detail.document ? (
            <>
              <div className="document-paper">
                <MarkdownView content={detail.document.markdown} />
                <footer className="document-hash">
                  <span>CONTENT HASH</span>
                  <code>{detail.document.content_hash}</code>
                </footer>
              </div>
              {detail.task.task_status === "COMPLETED" && (
                <ExportPanel
                  taskId={taskId}
                  taskVersion={detail.task.version}
                />
              )}
            </>
          ) : (
            <div className="document-empty">
              <div className="document-empty-mark">§</div>
              <h2>文档正在形成</h2>
              <p>确认单元会依次汇入这里。未确认内容不会进入最终 PRD。</p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function SectionTitle({ index, title }: { index: string; title: string }) {
  return (
    <div className="section-title">
      <span>{index}</span>
      <h2>{title}</h2>
    </div>
  );
}

function WorkflowCard({
  eyebrow,
  title,
  status,
  accent = false,
  children,
}: {
  eyebrow: string;
  title: string;
  status: string;
  accent?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className={`workflow-card ${accent ? "accent-card" : ""}`}>
      <div className="card-topline">
        <span>{eyebrow}</span>
        <i>{status.replaceAll("_", " ")}</i>
      </div>
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function Brief({ brief }: { brief: Record<string, unknown> }) {
  const rows = [
    ["目标用户", brief.target_users],
    ["使用场景", brief.scenarios],
    ["目标", brief.goals],
    ["范围内", brief.scope_in],
    ["范围外", brief.scope_out],
  ];
  return (
    <dl className="brief-grid">
      {rows.map(([label, value]) => (
        <div key={label as string}>
          <dt>{label as string}</dt>
          <dd>
            {Array.isArray(value) && value.length > 0
              ? value.join(" · ")
              : "待补充"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function UnitRail({
  units,
  currentSequence,
}: {
  units: TaskDetail["outline"] extends infer O
    ? NonNullable<O> extends { units: infer U }
      ? U
      : never
    : never;
  currentSequence: number | null;
}) {
  return (
    <ol className="unit-rail">
      {units.map((unit) => (
        <li
          className={unit.sequence === currentSequence ? "current" : ""}
          key={unit.unit_id}
        >
          <span>{unit.sequence}</span>
          <div>
            <strong>{unit.title}</strong>
            <small>{unit.status.replaceAll("_", " ")}</small>
          </div>
        </li>
      ))}
    </ol>
  );
}

function locatorLabel(locator: Record<string, unknown>): string {
  const path = typeof locator.path === "string" ? locator.path : null;
  const section = Array.isArray(locator.section_path)
    ? locator.section_path.join(" / ")
    : null;
  const lineStart =
    typeof locator.line_start === "number" ? locator.line_start : null;
  const lineEnd =
    typeof locator.line_end === "number" ? locator.line_end : null;
  if (path) {
    return `${path}${lineStart ? `:${lineStart}${lineEnd && lineEnd !== lineStart ? `-${lineEnd}` : ""}` : ""}`;
  }
  return section || "安全来源定位";
}

export function InvestigationCard({
  investigation,
}: {
  investigation: Investigation;
}) {
  return (
    <section
      className="investigation-card"
      aria-label={`调查：${investigation.question}`}
    >
      <div className="investigation-head">
        <div>
          <span>INVESTIGATION · {investigation.requiredness}</span>
          <strong>{investigation.question}</strong>
        </div>
        <i>{investigation.status}</i>
      </div>
      <div className="coverage-list" aria-label="调查覆盖度">
        {investigation.coverage.map((item) => (
          <div key={item.key}>
            <span>{item.description}</span>
            <strong data-status={item.status}>{item.status}</strong>
          </div>
        ))}
      </div>
      {investigation.tool_calls.map((call) => (
        <div className="tool-trace" key={call.tool_call_id}>
          <code>{call.tool_id}</code>
          <span>{call.public_summary || call.purpose}</span>
          <strong>{call.status}</strong>
        </div>
      ))}
      {investigation.evidence.slice(0, 5).map((evidence) => (
        <details className="evidence-trace" key={evidence.evidence_id}>
          <summary>
            <span>{evidence.source_kind.replaceAll("_", " ")}</span>
            <code>{locatorLabel(evidence.locator)}</code>
          </summary>
          <p>{evidence.excerpt}</p>
          <small>
            {evidence.extraction_method}
            {evidence.redaction_applied ? " · 已脱敏" : ""}
          </small>
        </details>
      ))}
      {investigation.unknowns.map((unknown) => (
        <div className="trace-alert unknown" key={unknown.unknown_id}>
          <strong>UNKNOWN</strong>
          <span>{unknown.statement}</span>
        </div>
      ))}
      {investigation.conflicts.map((conflict) => (
        <div className="trace-alert conflict" key={conflict.conflict_id}>
          <strong>CONFLICT</strong>
          <span>{conflict.description}</span>
        </div>
      ))}
      <footer>
        Stop reason · {investigation.stop_reason ?? "RUNNING"}
      </footer>
    </section>
  );
}

function WorkbenchSkeleton() {
  return (
    <div className="workbench skeleton-workbench" aria-label="任务加载中">
      <div className="skeleton-line wide" />
      <div className="skeleton-line" />
      <div className="skeleton-grid">
        <div />
        <div />
        <div />
      </div>
    </div>
  );
}
