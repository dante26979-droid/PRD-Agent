import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MarkdownView } from "./markdown-view";

describe("MarkdownView", () => {
  it("renders GFM content", () => {
    render(
      <MarkdownView
        content={"# PRD\n\n- 可确认\n\n| 字段 | 值 |\n| --- | --- |\n| 状态 | 完成 |"}
      />,
    );

    expect(screen.getByRole("heading", { name: "PRD" })).toBeInTheDocument();
    expect(screen.getByText("可确认")).toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });

  it("does not create script nodes or dangerous links", () => {
    const { container } = render(
      <MarkdownView
        content={'<script>alert("x")</script>\n\n[危险](javascript:alert(1))'}
      />,
    );

    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByText("危险").closest("a")).not.toHaveAttribute("href");
  });
});
