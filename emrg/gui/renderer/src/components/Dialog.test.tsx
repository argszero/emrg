import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog } from "./Dialog";

/**
 * Dialog.test.tsx — 共享模态框外壳测试（Batch 4 slice 1）。
 * 验证 open 受控渲染、ESC 关闭、监听清理。
 */

describe("Dialog", () => {
  it("open=false → 不渲染", () => {
    render(<Dialog open={false} title="T" />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("open=true → 渲染标题/正文/actions", () => {
    render(
      <Dialog open title="删除模型" actions={<button type="button">OK</button>}>
        确定删除？
      </Dialog>,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("删除模型")).toBeInTheDocument();
    expect(screen.getByText("确定删除？")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "OK" })).toBeInTheDocument();
  });

  it("无标题 → 不渲染 h2", () => {
    render(<Dialog open>正文</Dialog>);
    expect(screen.queryByRole("heading")).toBeNull();
  });

  it("ESC → onClose（对应 vanilla <dialog> cancel 事件）", async () => {
    const onClose = vi.fn();
    render(<Dialog open onClose={onClose}>x</Dialog>);
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("关闭后 ESC 监听移除（不再触发 onClose）", async () => {
    const onClose = vi.fn();
    const { rerender } = render(<Dialog open onClose={onClose}>x</Dialog>);
    rerender(<Dialog open={false} onClose={onClose}>x</Dialog>);
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("自定义 testId 生效（多对话框区分）", () => {
    render(<Dialog open testId="custom-dialog">x</Dialog>);
    expect(screen.getByTestId("custom-dialog")).toBeInTheDocument();
  });
});
