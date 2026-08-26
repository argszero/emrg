import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WelcomeDialog } from "./WelcomeDialog";
import { I18nProvider } from "../lib/i18n";

/**
 * WelcomeDialog.test.tsx — 首启引导对话框测试（Batch 4 slice 3）。
 * 镜像 vanilla showWelcome/saveWelcome：表单重置、模型下拉、空 Key 校验、保存回调。
 */

function setup(props: Partial<Parameters<typeof WelcomeDialog>[0]> = {}) {
  return render(
    <I18nProvider lang="zh">
      <WelcomeDialog open models={["deepseek-chat", "gpt-4o"]} {...props} />
    </I18nProvider>,
  );
}

describe("WelcomeDialog", () => {
  it("open=false → 不渲染", () => {
    render(
      <I18nProvider lang="zh">
        <WelcomeDialog open={false} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("welcome-dialog")).toBeNull();
  });

  it("渲染标题 + 表单（apiKey/baseUrl/model 下拉）", () => {
    setup({ defaultModel: "gpt-4o" });
    expect(screen.getByTestId("welcome-dialog")).toBeInTheDocument();
    expect(screen.getByText("欢迎使用 EMRG")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-api-key")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-base-url")).toBeInTheDocument();
    expect(screen.getByTestId("welcome-model")).toHaveValue("gpt-4o");
  });

  it("models 空 → fallback 列表（deepseek-chat 为首选）", () => {
    setup({ models: [] });
    expect(screen.getByTestId("welcome-model")).toHaveValue("deepseek-chat");
  });

  it("空 API Key → 内联错误，不触发 onSave", async () => {
    const onSave = vi.fn();
    setup({ onSave });
    await userEvent.click(screen.getByTestId("welcome-save"));
    expect(screen.getByTestId("welcome-error")).toHaveTextContent("API Key");
    expect(onSave).not.toHaveBeenCalled();
  });

  it("填写并保存 → onSave({ apiKey, baseUrl, model })", async () => {
    const onSave = vi.fn();
    setup({ onSave });
    await userEvent.type(screen.getByTestId("welcome-api-key"), "sk-test");
    await userEvent.type(screen.getByTestId("welcome-base-url"), "https://api.openai.com/v1");
    await userEvent.selectOptions(screen.getByTestId("welcome-model"), "gpt-4o");
    await userEvent.click(screen.getByTestId("welcome-save"));
    expect(onSave).toHaveBeenCalledWith({
      apiKey: "sk-test",
      baseUrl: "https://api.openai.com/v1",
      model: "gpt-4o",
    });
  });

  it("取消 → onDismiss", async () => {
    const onDismiss = vi.fn();
    setup({ onDismiss });
    await userEvent.click(screen.getByTestId("welcome-cancel"));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
