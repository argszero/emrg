import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryDialog } from "./MemoryDialog";
import { I18nProvider } from "../lib/i18n";
import type { MemoryRow } from "../lib/dialogLists";

/**
 * MemoryDialog.test.tsx — /memory 记忆浏览器对话框测试（Batch 4 slice 2）。
 * 镜像 vanilla showMemoryDialog：loading/空/列表/错误四态 + 详情块 + 点击行读详情。
 */

const memories: MemoryRow[] = [
  { id: "m1", title: "架构决策", summary: "微内核 + 工具调度" },
  { id: "m2", title: "T".repeat(50), summary: "S".repeat(60) },
];

function setup(props: Partial<Parameters<typeof MemoryDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <MemoryDialog open memories={memories} {...props} />
    </I18nProvider>,
  );
}

describe("MemoryDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <MemoryDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("memory-dialog")).toBeNull();
  });

  it("loading（memories=null）→ dlg.loading 行", () => {
    setup({ memories: null });
    expect(screen.getByTestId("memory-loading")).toHaveTextContent("加载中");
  });

  it("空列表 → app.noMemories（project scope 文案）", () => {
    setup({ memories: [] });
    expect(screen.getByTestId("memory-empty")).toHaveTextContent("项目");
  });

  it("空列表（session scope）→ session 文案", () => {
    setup({ memories: [], scope: "session" });
    expect(screen.getByTestId("memory-empty")).toHaveTextContent("会话");
  });

  it("列表：每行 label(40 截断) + hint(50 截断)", () => {
    setup();
    const rows = screen.getAllByTestId("memory-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("架构决策");
    expect(rows[0]).toHaveTextContent("微内核 + 工具调度");
    expect(rows[1].querySelector(".help-cmd")).toHaveTextContent("T".repeat(40));
    expect(rows[1].querySelector(".help-hint")).toHaveTextContent("S".repeat(50));
  });

  it("点击行 → onSelect(id)", async () => {
    const onSelect = vi.fn();
    setup({ onSelect });
    await userEvent.click(screen.getAllByTestId("memory-row")[1]);
    expect(onSelect).toHaveBeenCalledWith("m2");
  });

  it("error → app.memFailed 行", () => {
    setup({ error: "boom" });
    expect(screen.getByTestId("memory-error")).toHaveTextContent("加载记忆失败：boom");
  });

  it("detail 传入 → 渲染详情块（title + pre body）", () => {
    setup({ detail: { title: "架构决策", body: "微内核设计\n- 工具调度" } });
    const detail = screen.getByTestId("memory-detail");
    expect(detail).toBeInTheDocument();
    expect(detail.querySelector(".memory-detail-title")).toHaveTextContent("架构决策");
    // pre 文本换行被 jsdom 归一化为空格
    expect(detail.querySelector(".memory-detail-body")).toHaveTextContent("微内核设计 - 工具调度");
  });

  it("关闭按钮 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("dlg-close"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
