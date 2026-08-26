import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HelpDialog, helpRows } from "./HelpDialog";
import { I18nProvider } from "../lib/i18n";

/**
 * HelpDialog.test.tsx — /help 帮助对话框测试（Batch 4 slice 2）。
 * 镜像 vanilla showHelpDialog：COMMANDS 全量列出 + i18n hint + 关闭。
 */

const fakeCommands = {
  "/help": {},
  "/clear": {},
  "/memory": {},
};

function setup(props: Partial<Parameters<typeof HelpDialog>[0]> = {}) {
  const rows = helpRows(fakeCommands, (cmd) => `hint:${cmd}`);
  return render(
    <I18nProvider lang="zh">
      <HelpDialog open rows={rows} {...props} />
    </I18nProvider>,
  );
}

describe("HelpDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <HelpDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("help-dialog")).toBeNull();
  });

  it("渲染标题 + 描述 + 每行 cmd/hint", () => {
    setup();
    expect(screen.getByTestId("help-dialog")).toBeInTheDocument();
    expect(screen.getByText("/ 指令帮助")).toBeInTheDocument();
    const rows = screen.getAllByTestId("help-row");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("/help");
    expect(rows[0]).toHaveTextContent("hint:/help");
  });

  it("关闭按钮 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("dlg-close"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
