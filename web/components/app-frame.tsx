"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { listTasks } from "@/lib/api/client";
import type { TaskSummary } from "@/lib/api/types";

const statusLabel: Record<string, string> = {
  NEEDS_INPUT: "待输入",
  IN_PROGRESS: "进行中",
  AWAITING_CONFIRMATION: "待确认",
  COMPLETED: "已完成",
  FAILED: "失败",
  STOPPED: "已停止",
};

export function AppFrame({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const response = await listTasks();
      setTasks(response.items);
      setError("");
    } catch {
      setError("任务列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onChanged = () => void refresh();
    window.addEventListener("prd-agent:tasks-changed", onChanged);
    return () => window.removeEventListener("prd-agent:tasks-changed", onChanged);
  }, [refresh]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">PA</span>
          <div>
            <strong>PRD Agent</strong>
            <small>Grounded workbench</small>
          </div>
        </div>

        <Link className="new-task-button" href="/tasks/new">
          <span>＋</span> 新建 PRD
        </Link>

        <div className="sidebar-section-label">
          最近任务 <span>{tasks.length}</span>
        </div>
        <nav className="task-list" aria-label="PRD 任务">
          {loading && (
            <>
              <div className="task-skeleton" />
              <div className="task-skeleton" />
            </>
          )}
          {error && (
            <button className="sidebar-error" onClick={() => void refresh()}>
              {error} · 重试
            </button>
          )}
          {!loading && !error && tasks.length === 0 && (
            <p className="sidebar-empty">创建第一项任务，开始把模糊想法变成可验收需求。</p>
          )}
          {tasks.map((task) => {
            const active = pathname === `/tasks/${task.task_id}`;
            return (
              <Link
                className={`task-link ${active ? "active" : ""}`}
                href={`/tasks/${task.task_id}`}
                key={task.task_id}
                aria-current={active ? "page" : undefined}
              >
                <span className={`status-dot status-${task.display_status}`} />
                <span className="task-link-copy">
                  <strong title={task.title}>{task.title}</strong>
                  <small>
                    {statusLabel[task.display_status] ?? task.display_status}
                    <span> · </span>
                    {new Intl.DateTimeFormat("zh-CN", {
                      month: "numeric",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    }).format(new Date(task.updated_at))}
                  </small>
                </span>
              </Link>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <span className="local-indicator" />
          Portfolio Local
        </div>
      </aside>
      <main className="main-stage">{children}</main>
    </div>
  );
}
