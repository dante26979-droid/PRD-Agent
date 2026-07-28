import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { NewTask } from "./new-task";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/api/client", () => ({
  ApiError: class ApiError extends Error {},
  startTask: vi.fn(),
}));

describe("NewTask", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps submit disabled for an empty requirement", () => {
    render(<NewTask />);

    expect(screen.getByRole("button", { name: "开始梳理 →" })).toBeDisabled();
  });

  it("can populate the composer from an example", () => {
    render(<NewTask />);

    fireEvent.click(
      screen.getByRole("button", { name: "为订单列表增加创建时间筛选" }),
    );

    expect(screen.getByLabelText("需求描述")).toHaveValue(
      "为订单列表增加创建时间筛选",
    );
    expect(screen.getByRole("button", { name: "开始梳理 →" })).toBeEnabled();
  });
});
