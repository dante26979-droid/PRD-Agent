"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";

import { ApiError, startTask } from "@/lib/api/client";

const prompts = [
  "为订单列表增加创建时间筛选",
  "设计优惠券叠加使用规则",
  "优化售后退款审批流程",
];

export function NewTask() {
  const router = useRouter();
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const content = message.trim();
    if (!content || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      const detail = await startTask(content);
      window.dispatchEvent(new Event("prd-agent:tasks-changed"));
      router.push(`/tasks/${detail.task.task_id}`);
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? `${reason.message}（${reason.body?.correlation_id ?? "no-id"}）`
          : "无法创建任务，请检查 API 是否已启动。",
      );
      setSubmitting(false);
    }
  }

  return (
    <section className="new-task-page">
      <div className="eyebrow">NEW PRODUCT REQUIREMENT</div>
      <h1>从一个真实问题开始。</h1>
      <p className="lead">
        不必先写完整方案。描述背景、目标用户和要解决的问题，Agent 会把信息整理为可确认的大纲。
      </p>
      <form className="composer-card" onSubmit={submit}>
        <label htmlFor="requirement">需求描述</label>
        <textarea
          id="requirement"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="例如：订单运营需要按创建时间筛选订单，目前只能逐页查找……"
          rows={8}
          maxLength={20_000}
          autoFocus
        />
        <div className="composer-footer">
          <span>{message.length.toLocaleString()} / 20,000</span>
          <button
            className="primary-button"
            disabled={!message.trim() || submitting}
            type="submit"
          >
            {submitting ? "正在分析…" : "开始梳理 →"}
          </button>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
      </form>
      <div className="prompt-row" aria-label="示例需求">
        {prompts.map((prompt) => (
          <button key={prompt} onClick={() => setMessage(prompt)}>
            {prompt}
          </button>
        ))}
      </div>
      <div className="trust-row">
        <span>01 明确范围</span>
        <span>02 按单元确认</span>
        <span>03 来源可追溯</span>
      </div>
    </section>
  );
}
