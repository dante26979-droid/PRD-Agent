import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InvestigationCard } from "./task-workbench";

describe("InvestigationCard", () => {
  it("shows public coverage, evidence and stop reason", () => {
    render(
      <InvestigationCard
        investigation={{
          investigation_id: "investigation-1",
          information_need_id: "need-1",
          unit_id: "unit-1",
          question: "订单状态如何实现？",
          requiredness: "REQUIRED",
          source_types: ["CODE"],
          status: "COMPLETE",
          stop_reason: "COVERAGE_COMPLETE",
          coverage: [
            {
              key: "repository_structure",
              description: "相关模块已定位",
              status: "COVERED",
              evidence_ids: ["evidence-1"],
              fact_ids: [],
              unknown_ids: [],
              conflict_ids: [],
            },
          ],
          steps: [],
          tool_calls: [
            {
              tool_call_id: "call-1",
              tool_id: "repo_tree",
              status: "SUCCEEDED",
              purpose: "定位模块",
              public_summary: "返回 3 个文件",
              error_code: null,
              started_at: "2026-07-27T00:00:00Z",
              ended_at: "2026-07-27T00:00:01Z",
            },
          ],
          evidence: [
            {
              evidence_id: "evidence-1",
              source_kind: "CODE_REPOSITORY",
              source_id: "demo",
              source_version: "a".repeat(40),
              locator: { path: "src/order.py", line_start: 3 },
              excerpt: "订单状态定义",
              content_hash: "sha256:test",
              extraction_method: "SOURCE_READ",
              redaction_applied: true,
            },
          ],
          facts: [],
          unknowns: [],
          conflicts: [],
        }}
      />,
    );

    expect(screen.getByText("订单状态如何实现？")).toBeInTheDocument();
    expect(screen.getByText("相关模块已定位")).toBeInTheDocument();
    expect(screen.getByText("src/order.py:3")).toBeInTheDocument();
    expect(screen.getByText(/COVERAGE_COMPLETE/)).toBeInTheDocument();
    expect(screen.getByText(/已脱敏/)).toBeInTheDocument();
  });
});
