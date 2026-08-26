import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RenameDialog } from "./RenameDialog";
import { I18nProvider } from "../lib/i18n";
import type { RenameRequest } from "../lib/dialog";

/**
 * RenameDialog.test.tsx — 重命名对话对话框测试（Batch 4 slice 1）。
 * 镜像 vanilla showRename/submitRename 语义：预填规则（currentTitle ≠ sid 才填）、
 * 空名不提交、Enter 提交、取消/ESC 关闭。
 */

function setup(request: RenameRequest | null, props: Partial<Parameters<typeof RenameDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <RenameDialog request={request} {...props} />
    </I18nProvider>,
  );
}

describe("RenameDialog", () => {
  it("request=null → 不渲染", () => {
    setup(null);
    expect(screen.queryByTestId("rename-dialog")).toBeNull();
  });

  it("渲染标题/输入框/保存取消按钮", () => {
    setup({ sessionId: "s1" });
    expect(screen.getByTestId("rename-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("rename-input")).toBeInTheDocument();
    expect(screen.getByTestId("rename-ok")).toHaveTextContent("保存");
    expect(screen.getByTestId("rename-cancel")).toHaveTextContent("取消");
  });

  it("currentTitle ≠ sid → 预填标题", () => {
    setup({ sessionId: "s1", currentTitle: "周报草稿" });
    expect(screen.getByTestId("rename-input")).toHaveValue("周报草稿");
  });

  it("currentTitle === sid → 输入框留空（未命名会话，vanilla 语义）", () => {
    setup({ sessionId: "s1", currentTitle: "s1" });
    expect(screen.getByTestId("rename-input")).toHaveValue("");
  });

  it("空名提交 → onSubmit 不调用（vanilla 空名不提交）", async () => {
    const onSubmit = vi.fn();
    setup({ sessionId: "s1", currentTitle: "" }, { onSubmit });
    await userEvent.click(screen.getByTestId("rename-ok"));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("输入标题点保存 → onSubmit(sid, title) + onDismiss", async () => {
    const onSubmit = vi.fn();
    const onDismiss = vi.fn();
    setup({ sessionId: "s1" }, { onSubmit, onDismiss });
    await userEvent.type(screen.getByTestId("rename-input"), "新标题");
    await userEvent.click(screen.getByTestId("rename-ok"));
    expect(onSubmit).toHaveBeenCalledWith("s1", "新标题");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("Enter 键提交（vanilla rename 键盘提交）", async () => {
    const onSubmit = vi.fn();
    setup({ sessionId: "s1" }, { onSubmit });
    await userEvent.type(screen.getByTestId("rename-input"), "回车提交");
    await userEvent.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("s1", "回车提交");
  });

  it("点击取消 → 仅 onDismiss（onSubmit 不调用）", async () => {
    const onSubmit = vi.fn();
    const onDismiss = vi.fn();
    setup({ sessionId: "s1" }, { onSubmit, onDismiss });
    await userEvent.click(screen.getByTestId("rename-cancel"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("ESC → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ sessionId: "s1" }, { onDismiss });
    await userEvent.keyboard("{Escape}");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
