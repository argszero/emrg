import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RewindDialog } from "./RewindDialog";
import { I18nProvider } from "../lib/i18n";
import type { RewindPoint } from "../lib/rewind";

/**
 * RewindDialog.test.tsx — /rewind 历史回退对话框测试（Batch 4 slice 4）。
 * 镜像 vanilla showRewindDialog：loading/空/error/列表四态 + 行点击 + 关闭。
 */

const points: RewindPoint[] = [
  { record_index: 1, preview: "first message" },
  { record_index: 2, preview: "second message" },
  { record_index: 3, preview: "third message" },
];

function setup(props: Partial<Parameters<typeof RewindDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <RewindDialog open points={points} {...props} />
    </I18nProvider>,
  );
}

describe("RewindDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <RewindDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("rewind-dialog")).toBeNull();
  });

  it("渲染标题 + 描述", () => {
    setup();
    expect(screen.getByTestId("rewind-dialog")).toBeInTheDocument();
    expect(screen.getByText("回退到历史消息点")).toBeInTheDocument();
    expect(screen.getByText("选择要保留到的消息点，之后的对话将被移除。")).toBeInTheDocument();
  });

  it("points=null → loading 行", () => {
    setup({ points: null });
    expect(screen.getByTestId("rewind-loading")).toHaveTextContent("加载中…");
  });

  it("points=[] → 无历史文案", () => {
    setup({ points: [] });
    expect(screen.getByTestId("rewind-empty")).toHaveTextContent("没有可回退的历史消息。");
  });

  it("列表倒序（最新在上）+ 行 label/hint", () => {
    setup();
    const rows = screen.getAllByTestId("rewind-point");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("#3");
    expect(rows[0]).toHaveTextContent("third message");
    expect(rows[2]).toHaveTextContent("#1");
    expect(rows[2]).toHaveTextContent("first message");
  });

  it("点击消息点 → onPick(record_index)", async () => {
    const onPick = vi.fn();
    setup({ onPick });
    await userEvent.click(screen.getAllByTestId("rewind-point")[1]);
    expect(onPick).toHaveBeenCalledWith(2);
  });

  it("error → 历史加载失败文案", () => {
    setup({ error: "boom" });
    expect(screen.getByTestId("rewind-error")).toHaveTextContent("加载历史失败：boom");
  });

  it("关闭按钮（rewind.cancel 文案）→ onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    const close = screen.getByTestId("rewind-close");
    expect(close).toHaveTextContent("取消");
    await userEvent.click(close);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
