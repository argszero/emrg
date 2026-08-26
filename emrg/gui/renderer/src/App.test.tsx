import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { App } from "./App";

describe("App shell (Batch 0 smoke)", () => {
  it("renders the React skeleton with the batch badge", () => {
    render(<App />);
    // 骨架存在（设计 §5 Batch 0 项 5：挂载 App 断言骨架存在）
    expect(screen.getByTestId("react-shell")).toBeInTheDocument();
    expect(screen.getByTestId("react-shell-badge")).toHaveTextContent("EMRG React shell");
  });

  it("renders inside the error boundary", () => {
    const { container } = render(<App />);
    // 无错误时错误边界不渲染覆盖层（零干扰）
    expect(container.querySelector(".error-boundary-overlay")).not.toBeInTheDocument();
  });
});
