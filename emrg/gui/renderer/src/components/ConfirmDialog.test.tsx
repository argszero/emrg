import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConfirmDialog } from "./ConfirmDialog";
import { I18nProvider } from "../lib/i18n";
import type { ConfirmRequest } from "../lib/dialog";

/**
 * ConfirmDialog.test.tsx — 确认对话框测试（Batch 4 slice 1）。
 * 镜像 vanilla showConfirm 语义：默认删除确认（danger）、danger:false → primary、
 * OK → onOk + 关闭、取消/ESC → 仅关闭。
 */

function setup(request: ConfirmRequest | null, props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <ConfirmDialog request={request} {...props} />
    </I18nProvider>,
  );
}

describe("ConfirmDialog", () => {
  it("request=null → 不渲染", () => {
    setup(null);
    expect(screen.queryByTestId("confirm-dialog")).toBeNull();
  });

  it("渲染 title + message", () => {
    setup({ title: "删除模型", message: "确定删除吗？" });
    expect(screen.getByTestId("confirm-dialog")).toBeInTheDocument();
    expect(screen.getByText("删除模型")).toBeInTheDocument();
    expect(screen.getByText("确定删除吗？")).toBeInTheDocument();
  });

  it("默认 OK：btn-danger + confirm.delete 文案（vanilla 默认删除确认）", () => {
    setup({ title: "T", message: "M" });
    const ok = screen.getByTestId("confirm-ok");
    expect(ok).toHaveClass("btn-danger");
    expect(ok).toHaveTextContent("删除");
  });

  it("danger:false → btn-primary + settings.save", () => {
    setup({ title: "T", message: "M", danger: false });
    const ok = screen.getByTestId("confirm-ok");
    expect(ok).toHaveClass("btn-primary");
    expect(ok).toHaveTextContent("保存");
  });

  it("显式 okText 透传（已译字符串原样显示）", () => {
    setup({ title: "T", message: "M", okText: "搞定" });
    expect(screen.getByTestId("confirm-ok")).toHaveTextContent("搞定");
  });

  it("点击 OK → onOk 调用 + onDismiss 调用", async () => {
    const onOk = vi.fn();
    const onDismiss = vi.fn();
    setup({ title: "T", message: "M", onOk }, { onDismiss });
    await userEvent.click(screen.getByTestId("confirm-ok"));
    expect(onOk).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("点击取消 → 仅 onDismiss（onOk 不调用）", async () => {
    const onOk = vi.fn();
    const onDismiss = vi.fn();
    setup({ title: "T", message: "M", onOk }, { onDismiss });
    await userEvent.click(screen.getByTestId("confirm-cancel"));
    expect(onOk).not.toHaveBeenCalled();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("ESC → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ title: "T", message: "M" }, { onDismiss });
    await userEvent.keyboard("{Escape}");
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
