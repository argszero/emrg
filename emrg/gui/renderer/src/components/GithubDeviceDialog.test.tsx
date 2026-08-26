import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GithubDeviceDialog } from "./GithubDeviceDialog";
import { I18nProvider } from "../lib/i18n";

/**
 * GithubDeviceDialog.test.tsx — GitHub device-flow 授权对话框测试（Batch 4 slice 4）。
 * 镜像 vanilla startDeviceFlow/initDeviceDialog：code 展示 + 打开浏览器 + 等待轮询 + 取消。
 */

function setup(props: Partial<Parameters<typeof GithubDeviceDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <GithubDeviceDialog open code="ABCD-1234" url="https://github.com/login/device" {...props} />
    </I18nProvider>,
  );
}

describe("GithubDeviceDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <GithubDeviceDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("github-device-dialog")).toBeNull();
  });

  it("渲染标题 + 描述 + 一次性授权码", () => {
    setup();
    expect(screen.getByTestId("github-device-dialog")).toBeInTheDocument();
    expect(screen.getByText("连接 GitHub")).toBeInTheDocument();
    expect(screen.getByTestId("github-device-code")).toHaveTextContent("ABCD-1234");
  });

  it("code=null → 占位符 —（vanilla 初始 textContent）", () => {
    setup({ code: null });
    expect(screen.getByTestId("github-device-code")).toHaveTextContent("—");
  });

  it("打开浏览器按钮 → onOpenBrowser", async () => {
    const onOpenBrowser = vi.fn();
    setup({ onOpenBrowser });
    await userEvent.click(screen.getByTestId("github-device-open"));
    expect(onOpenBrowser).toHaveBeenCalledTimes(1);
  });

  it("取消按钮（githubDeviceCancel 文案）→ onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    const close = screen.getByTestId("github-device-close");
    expect(close).toHaveTextContent("取消");
    await userEvent.click(close);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("waiting=true → 显示等待确认提示；false → 隐藏", () => {
    const view = setup({ waiting: true });
    expect(screen.getByTestId("github-device-wait")).toHaveTextContent("等待浏览器中确认…");
    view.unmount();
    setup({ waiting: false });
    expect(screen.queryByTestId("github-device-wait")).toBeNull();
  });

  it("error → GitHub 连接失败文案", () => {
    setup({ error: "network down" });
    expect(screen.getByTestId("github-device-error")).toHaveTextContent("GitHub 连接失败：network down");
  });
});
