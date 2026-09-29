import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApprovalDialog } from "./ApprovalDialog";
import { I18nProvider } from "../lib/i18n";
import type { PendingApproval } from "../lib/daemonBridge";

/**
 * ApprovalDialog.test.tsx — 沙箱提权提问对话框（rant 2026-09-29T15:52:38，要求 1）。
 *
 * 这些断言守的不是版式而是**语义**：提问原样显示、两条出口各送出它对应的那个
 * 布尔值，且「离开窗口」被算作拒绝而不是没有答复 —— daemon 侧 fail-closed，
 * 客户端这一半必须同向，否则「宿主走开了」在两端会读成两件事。
 */

const PENDING: PendingApproval = {
  requestId: "appr-abc123",
  question:
    "Approve a one-hop sandbox escalation for this command only?\n  read-only → workspace-write\n  justification: npm install",
  sessionId: "s1",
};

// onAnswer 是必填 prop（没有「不作答」这条路），所以夹具给一个显式缺省而不是让它
// 变成可选 —— 一个能省略 onAnswer 的夹具会允许一个没有出口的提问编译通过。
function setup(
  request: PendingApproval | null,
  props: { onAnswer?: (approved: boolean) => void } = {},
) {
  return render(
    <I18nProvider lang="zh">
      <ApprovalDialog request={request} onAnswer={props.onAnswer ?? (() => {})} />
    </I18nProvider>,
  );
}

describe("ApprovalDialog", () => {
  it("request=null → 不渲染（daemon 没有在问）", () => {
    setup(null);
    expect(screen.queryByTestId("approval-dialog")).toBeNull();
  });

  it("提问文本原样显示（换行保留，不重新排版）", () => {
    setup(PENDING);
    const q = screen.getByTestId("approval-question");
    expect(q.textContent).toBe(PENDING.question);
    expect(q.textContent).toContain("justification: npm install");
    expect((q as HTMLElement).style.whiteSpace).toBe("pre-wrap");
  });

  it("点批准 → onAnswer(true)", async () => {
    const onAnswer = vi.fn();
    setup(PENDING, { onAnswer });
    await userEvent.click(screen.getByTestId("approval-allow"));
    expect(onAnswer).toHaveBeenCalledWith(true);
  });

  it("点拒绝 → onAnswer(false)", async () => {
    const onAnswer = vi.fn();
    setup(PENDING, { onAnswer });
    await userEvent.click(screen.getByTestId("approval-deny"));
    expect(onAnswer).toHaveBeenCalledWith(false);
  });

  it("ESC 关闭 → onAnswer(false)：静默即拒绝，不是「没有答复」", async () => {
    const onAnswer = vi.fn();
    setup(PENDING, { onAnswer });
    await userEvent.keyboard("{Escape}");
    expect(onAnswer).toHaveBeenCalledWith(false);
  });

  it("界面自己说明「不回答就是拒绝」（宿主不能把超时读成它自己取消了）", () => {
    setup(PENDING);
    expect(screen.getByTestId("approval-hint").textContent).toContain("拒绝");
  });
});
