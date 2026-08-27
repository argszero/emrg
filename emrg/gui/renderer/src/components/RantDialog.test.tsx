import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { RantDialog } from "./RantDialog";
import type { ProjectRec } from "../lib/workspaceView";

/**
 * RantDialog.test.tsx — Batch 5 slice 8：Rant 提交对话框。
 * 镜像 vanilla openRantForm/submitRantForm 语义：打开清空 + 聚焦、消息必填、
 * 项目下拉（已注册项目）、提交 payload {message, project}。
 */

function wrapper(node: React.ReactNode) {
  return <I18nProvider lang="en">{node}</I18nProvider>;
}

const PROJECTS: ProjectRec[] = [
  { name: "emrg", path: "/p/emrg" },
  { name: "sci", path: "/p/sci" },
];

afterEach(() => {
  vi.restoreAllMocks();
});

describe("RantDialog", () => {
  it("open：渲染消息文本域 + 项目下拉（已注册项目）", () => {
    render(wrapper(<RantDialog open projects={PROJECTS} onDismiss={vi.fn()} />));
    expect(screen.getByTestId("rant-dialog")).toBeInTheDocument();
    expect((screen.getByTestId("rant-message") as HTMLTextAreaElement).value).toBe("");
    const sel = screen.getByTestId("rant-project") as HTMLSelectElement;
    expect(sel.options.length).toBe(PROJECTS.length + 1); // + 空选项
    expect(sel.options[1].value).toBe("emrg");
  });

  it("空消息 → 内联错误，不提交", () => {
    const onSubmit = vi.fn();
    render(wrapper(<RantDialog open projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
    fireEvent.click(screen.getByTestId("rant-submit"));
    expect(screen.getByTestId("rant-error")).toHaveTextContent("required");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("提交 payload {message, project}（选项目）", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(wrapper(<RantDialog open projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
    fireEvent.change(screen.getByTestId("rant-message"), { target: { value: "侧栏太宽了" } });
    fireEvent.change(screen.getByTestId("rant-project"), { target: { value: "emrg" } });
    fireEvent.click(screen.getByTestId("rant-submit"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toEqual({ message: "侧栏太宽了", project: "emrg" });
  });

  it("项目选填：留空提交 project=''", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(wrapper(<RantDialog open projects={PROJECTS} onSubmit={onSubmit} onDismiss={vi.fn()} />));
    fireEvent.change(screen.getByTestId("rant-message"), { target: { value: "hello" } });
    fireEvent.click(screen.getByTestId("rant-submit"));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit.mock.calls[0][0]).toEqual({ message: "hello", project: "" });
  });

  it("closed (open=false) → 不渲染", () => {
    render(wrapper(<RantDialog open={false} onDismiss={vi.fn()} />));
    expect(screen.queryByTestId("rant-dialog")).not.toBeInTheDocument();
  });
});
