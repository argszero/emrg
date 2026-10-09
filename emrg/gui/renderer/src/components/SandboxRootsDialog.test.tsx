import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SandboxRootsDialog } from "./SandboxRootsDialog";
import { I18nProvider } from "../lib/i18n";

/**
 * SandboxRootsDialog.test.tsx — 会话额外可写根对话框（rant 2026-10-09T09:43:39, GUI half）。
 *
 * 三件必须被钉住的事，每一件都是这份组件存在的原因：
 *  1. 打开就问 daemon 要列表（`op=list`），因为列表是 daemon 的、而 list 帧只回答发问者；
 *  2. 输入与点击只**上报意图**——`add` / `remove` 各发一次 op，本地不留副本；
 *  3. 三种「没有列表」互不相同：无会话 / 没人问过（未知）/ 问过且为空。
 */

function setup(props: Partial<Parameters<typeof SandboxRootsDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <SandboxRootsDialog open sid="s1" {...props} />
    </I18nProvider>,
  );
}

describe("SandboxRootsDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <SandboxRootsDialog open={false} sid="s1" />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("sandbox-roots-dialog")).toBeNull();
  });

  it("打开即向 daemon 要一次列表（op=list，路径为空）", () => {
    const onOp = vi.fn();
    setup({ onOp });
    expect(onOp).toHaveBeenCalledWith("list", "");
  });

  it("没有激活会话 → 不问、不发命令，显示「先开一个会话」而不是借用别人的列表", () => {
    const onOp = vi.fn();
    setup({ sid: null, onOp, roots: ["/tmp/x"] });
    expect(onOp).not.toHaveBeenCalled();
    expect(screen.getByTestId("sandbox-roots-nosession")).toBeInTheDocument();
    // 传进来的列表在没有会话时不该被渲染：会话是这些根的**主语**。
    expect(screen.queryAllByTestId("sandbox-roots-row")).toHaveLength(0);
    expect(screen.getByTestId("sandbox-roots-path")).toBeDisabled();
    expect(screen.getByTestId("sandbox-roots-add")).toBeDisabled();
  });

  it("roots=null → 未知（加载行），不是空列表", () => {
    setup({ roots: null });
    expect(screen.getByTestId("sandbox-roots-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("sandbox-roots-empty")).toBeNull();
  });

  it("roots=[] → 明确为「还没有」，不是未知", () => {
    setup({ roots: [] });
    expect(screen.getByTestId("sandbox-roots-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("sandbox-roots-loading")).toBeNull();
  });

  it("每一行都渲染路径与一个移除按钮", () => {
    setup({ roots: ["/tmp/scratch", "/Users/x/notes"] });
    const rows = screen.getAllByTestId("sandbox-roots-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveAttribute("data-root", "/tmp/scratch");
    expect(rows[0]).toHaveTextContent("/tmp/scratch");
    expect(screen.getAllByTestId("sandbox-roots-remove")).toHaveLength(2);
  });

  it("移除按钮只发 op=remove + 该行的路径（本地不改自己那份）", async () => {
    const onOp = vi.fn();
    setup({ roots: ["/tmp/scratch"], onOp });
    onOp.mockClear();
    await userEvent.click(screen.getByTestId("sandbox-roots-remove"));
    expect(onOp).toHaveBeenCalledWith("remove", "/tmp/scratch");
    // 组件不持有列表：点完这一行仍在（只有 daemon 的回帧能让它消失）。
    expect(screen.getByTestId("sandbox-roots-row")).toBeInTheDocument();
  });

  it("输入路径 + 点添加 → op=add + 该路径，输入框清空", async () => {
    const onOp = vi.fn();
    setup({ roots: [], onOp });
    onOp.mockClear();
    const input = screen.getByTestId("sandbox-roots-path");
    await userEvent.type(input, "  /tmp/scratch  ");
    await userEvent.click(screen.getByTestId("sandbox-roots-add"));
    expect(onOp).toHaveBeenCalledWith("add", "/tmp/scratch");
    expect(input).toHaveValue("");
  });

  it("Enter 与点击等价", async () => {
    const onOp = vi.fn();
    setup({ roots: [], onOp });
    onOp.mockClear();
    await userEvent.type(screen.getByTestId("sandbox-roots-path"), "/tmp/a{Enter}");
    expect(onOp).toHaveBeenCalledWith("add", "/tmp/a");
  });

  it("空输入不发命令（daemon 会拒，但这条没在问它）", async () => {
    const onOp = vi.fn();
    setup({ roots: [], onOp });
    onOp.mockClear();
    await userEvent.type(screen.getByTestId("sandbox-roots-path"), "   {Enter}");
    expect(onOp).not.toHaveBeenCalled();
  });

  it("拒绝（error）→ 原样显示 daemon 的规则，且 role=alert", () => {
    setup({
      roots: [],
      notice: { kind: "error", text: "refusing '/': the filesystem root", op: "add" },
    });
    const notice = screen.getByTestId("sandbox-roots-notice");
    expect(notice).toHaveAttribute("data-kind", "error");
    expect(notice).toHaveAttribute("role", "alert");
    expect(notice).toHaveTextContent("refusing '/': the filesystem root");
  });

  it("notice（什么都没改）与 ok（改成了）用不同句子区分", () => {
    const { unmount } = setup({
      roots: ["/tmp/x"],
      notice: { kind: "notice", text: "'/tmp/x' was not one of this session's roots", op: "remove" },
    });
    expect(screen.getByTestId("sandbox-roots-notice")).toHaveTextContent(
      "'/tmp/x' was not one of this session's roots",
    );
    unmount();
    setup({ roots: ["/tmp/x"], notice: { kind: "ok", text: "", op: "add" } });
    expect(screen.getByTestId("sandbox-roots-notice")).toHaveTextContent("已更新额外可写根（add）");
  });

  it("没有 notice 时不渲染一行空话", () => {
    setup({ roots: [] });
    expect(screen.queryByTestId("sandbox-roots-notice")).toBeNull();
  });

  it("关闭按钮 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("sandbox-roots-close"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
