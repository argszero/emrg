import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ErrorBoundary } from "./ErrorBoundary";

function Bomb(): never {
  throw new Error("boom: intentional test failure");
}

describe("ErrorBoundary", () => {
  it("renders children when no error occurs", () => {
    render(
      <ErrorBoundary>
        <div>ok content</div>
      </ErrorBoundary>,
    );
    expect(screen.getByText("ok content")).toBeInTheDocument();
  });

  it("shows the full-screen overlay when a child throws", () => {
    // 预期内的 React 错误日志：静默避免测试输出噪音
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      render(
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>,
      );
      // 覆盖层：标题（zh/en）+ 错误摘要（截断 500 字符）+ role=alert
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/渲染器出错|Renderer error/)).toBeInTheDocument();
      expect(screen.getByText(/boom: intentional test failure/)).toBeInTheDocument();
      // 两按钮：复制错误 / 重新加载
      expect(screen.getByRole("button", { name: /复制错误|Copy error/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /重新加载|Reload/ })).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it("keeps the overlay mounted without auto-dismiss", () => {
    // 无自动重载（防崩溃循环，设计参考 grok-bot error-boundary）：渲染后覆盖层
    // 持久存在，不会自动消失（重载仅由用户点击按钮触发）。
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { rerender } = render(
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>,
      );
      rerender(
        <ErrorBoundary>
          <Bomb />
        </ErrorBoundary>,
      );
      expect(screen.getByRole("alert")).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });
});
