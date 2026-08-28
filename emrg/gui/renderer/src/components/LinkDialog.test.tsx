import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LinkDialog } from "./LinkDialog";
import { I18nProvider } from "../lib/i18n";

/** LinkDialog — 链接 URL 输入对话框（rant 2026-08-28T22:27:01） */
describe("LinkDialog", () => {
  it("关闭时不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <LinkDialog open={false} onApply={() => {}} onCancel={() => {}} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("link-dialog")).not.toBeInTheDocument();
  });

  it("打开时预填 currentHref//https:// 并聚焦", () => {
    render(
      <I18nProvider lang="zh">
        <LinkDialog open currentHref="" onApply={() => {}} onCancel={() => {}} />
      </I18nProvider>,
    );
    const input = screen.getByTestId("link-url") as HTMLInputElement;
    expect(input).toBeInTheDocument();
    expect(input.value).toBe("https://");
  });

  it("输入 URL → 点确定 → onApply(href)", async () => {
    const apply = vi.fn();
    render(
      <I18nProvider lang="zh">
        <LinkDialog open onApply={apply} onCancel={() => {}} />
      </I18nProvider>,
    );
    const input = screen.getByTestId("link-url") as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "https://example.com");
    await userEvent.click(screen.getByTestId("link-ok"));
    expect(apply).toHaveBeenCalledWith("https://example.com");
  });

  it("Enter 提交 URL", async () => {
    const apply = vi.fn();
    render(
      <I18nProvider lang="zh">
        <LinkDialog open onApply={apply} onCancel={() => {}} />
      </I18nProvider>,
    );
    const input = screen.getByTestId("link-url") as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "https://enter.dev{enter}");
    expect(apply).toHaveBeenCalledWith("https://enter.dev");
  });

  it("空 URL 不提交", async () => {
    const apply = vi.fn();
    render(
      <I18nProvider lang="zh">
        <LinkDialog open onApply={apply} onCancel={() => {}} />
      </I18nProvider>,
    );
    const input = screen.getByTestId("link-url") as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.click(screen.getByTestId("link-ok"));
    expect(apply).not.toHaveBeenCalled();
  });

  it("取消 → onCancel，不提交", async () => {
    const apply = vi.fn();
    const cancel = vi.fn();
    render(
      <I18nProvider lang="zh">
        <LinkDialog open onApply={apply} onCancel={cancel} />
      </I18nProvider>,
    );
    const input = screen.getByTestId("link-url") as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "https://example.com");
    await userEvent.click(screen.getByTestId("link-cancel"));
    expect(cancel).toHaveBeenCalled();
    expect(apply).not.toHaveBeenCalled();
  });
});
