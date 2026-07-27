import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExportPanel } from "./export-panel";
import {
  executeFeishuExport,
  listExports,
  previewFeishuExport,
} from "@/lib/api/client";

vi.mock("@/lib/api/client", () => ({
  executeFeishuExport: vi.fn(),
  listExports: vi.fn(),
  previewFeishuExport: vi.fn(),
}));

describe("ExportPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listExports).mockResolvedValue({ items: [] });
    vi.mocked(previewFeishuExport).mockResolvedValue({
      intent_id: "intent-1",
      confirmation_token: "confirmation-1",
      mode: "CREATE",
      title: "订单筛选",
      task_version: 7,
      document_version: 3,
      content_hash: `sha256:${"a".repeat(64)}`,
      unresolved_items: ["时区待确认"],
      expires_at: "2026-07-27T12:00:00Z",
      bound_title: null,
      bound_safe_url: null,
    });
    vi.mocked(executeFeishuExport).mockResolvedValue({
      export_run_id: "run-1",
      intent_id: "intent-1",
      mode: "CREATE",
      task_version: 7,
      document_version: 3,
      content_hash: `sha256:${"a".repeat(64)}`,
      status: "SUCCEEDED",
      attempt_count: 1,
      binding_id: "binding-1",
      safe_url: "https://example.feishu.cn/docx/safe",
      display_title: "订单筛选",
      error_code: null,
      retryable: false,
      created_at: "2026-07-27T10:00:00Z",
      completed_at: "2026-07-27T10:00:01Z",
    });
  });

  it("previews before explicit confirmation and then shows the safe document link", async () => {
    render(<ExportPanel taskId="task-1" taskVersion={7} />);

    fireEvent.click(await screen.findByRole("button", { name: "导出到飞书" }));
    expect(await screen.findByText("时区待确认")).toBeInTheDocument();
    expect(executeFeishuExport).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认创建" }));

    await waitFor(() => expect(executeFeishuExport).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("link", { name: "打开飞书文档" })).toHaveAttribute(
      "href",
      "https://example.feishu.cn/docx/safe",
    );
  });
});
