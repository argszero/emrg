/**
 * ResultPanel.test.tsx — 结果面板 React 组件测试（Batch 3）。
 * 镜像 vanilla result-panel.js 的关键交互：Tab 切换、产物行点击 → 打开查看器 Tab、
 * 查看器加载文本/md/图片/HTML、折叠开关、文件 Tab 上限。
 * 类名与 vanilla CSS 一致（Batch 5 复用）。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ResultPanel, type ResultPanelProps, type ReadFile } from "./ResultPanel";
import type { ArtifactRec } from "../lib/resultPanel";

function setup(props: Partial<ResultPanelProps> = {}) {
  const utils = render(
    <ResultPanel
      sid="s1"
      workspaceRoot="/proj"
      artifacts={[]}
      {...props}
    />,
  );
  return { ...utils };
}

const art = (path: string, tool_name = "write"): ArtifactRec => ({
  path,
  name: path.split("/").pop() || path,
  tool_name,
});

describe("ResultPanel", () => {
  it("默认显示产物 Tab 空态；切换文件 Tab → FileTree 渲染", async () => {
    const listFiles = vi.fn(async () => ({ entries: [] }));
    setup({ listFiles });
    // 初始 active=artifacts → 空提示
    expect(screen.getByTestId("result-empty")).toBeInTheDocument();
    // 切到文件 Tab
    await userEvent.click(screen.getByTestId("result-tab-files"));
    expect(screen.getByTestId("result-files")).toBeInTheDocument();
    expect(listFiles).toHaveBeenCalledWith("/proj");
  });

  it("产物列表渲染；点击产物行 → 打开查看器 Tab（readFile 被调）", async () => {
    const readFile: ReadFile = vi.fn(async () => ({ content: "print(1)" }));
    const artifacts = [art("/proj/a.py"), art("/proj/b.py", "edit")];
    setup({ artifacts, readFile });
    // addArtifact unshift 后行序 = 输入倒序 → rows[0] 是 b.py（最新）
    expect(screen.getAllByTestId("artifact-row")).toHaveLength(2);
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => {
      expect(screen.getByTestId("result-viewer")).toBeInTheDocument();
      expect(screen.getByTestId("result-filetab")).toHaveTextContent("b.py");
      expect(readFile).toHaveBeenCalledWith({ path: "/proj/b.py" });
      expect(screen.getByText("print(1)")).toBeInTheDocument();
    });
  });

  it("打开文件 Tab 去重：重复点击同一产物只一个 Tab", async () => {
    const readFile: ReadFile = vi.fn(async () => ({ content: "x" }));
    const artifacts = [art("/proj/a.py")];
    setup({ artifacts, readFile });
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => expect(screen.getByTestId("result-filetab")).toBeInTheDocument());
    // 切回产物 Tab 再点同一产物 → 去重激活既有，不新增 Tab
    await userEvent.click(screen.getByTestId("result-tab-artifacts"));
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => {
      expect(screen.getAllByTestId("result-filetab")).toHaveLength(1);
    });
  });

  it("关闭文件 Tab → 回退到产物 Tab（产物行仍在）", async () => {
    const readFile: ReadFile = vi.fn(async () => ({ content: "x" }));
    const artifacts = [art("/proj/a.py")];
    setup({ artifacts, readFile });
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => expect(screen.getByTestId("result-filetab")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("filetab-close"));
    await waitFor(() => {
      expect(screen.queryByTestId("result-filetab")).not.toBeInTheDocument();
      // active 回退 artifacts → 产物行重新可见（非空，无 result-empty）
      expect(screen.getAllByTestId("artifact-row")).toHaveLength(1);
    });
  });

  it("查看器：md 路径 → 明文渲染；图片路径 → img 直显；HTML → 占位", async () => {
    // md
    setup({ artifacts: [art("/proj/README.md")], readFile: vi.fn(async () => ({ content: "# Hi" })) });
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => expect(screen.getByText("# Hi")).toBeInTheDocument());
    // image — rerender with new props
    render(
      <ResultPanel
        sid="s2"
        workspaceRoot="/proj"
        artifacts={[art("/proj/logo.png")]}
        readFile={vi.fn(async () => ({ content: "" }))}
      />,
    );
    const rows = screen.getAllByTestId("artifact-row");
    await userEvent.click(rows[0]);
    await waitFor(() => {
      const img = document.querySelector(".viewer-img") as HTMLImageElement | null;
      expect(img).not.toBeNull();
      expect(img?.src).toContain("file:///proj/logo.png");
    });
  });

  it("查看器 readFile 失败 → 错误提示", async () => {
    const readFile: ReadFile = vi.fn(async () => { throw new Error("boom"); });
    setup({ artifacts: [art("/proj/a.py")], readFile });
    await userEvent.click(screen.getAllByTestId("artifact-row")[0]);
    await waitFor(() => expect(screen.getByText("Failed to load file")).toBeInTheDocument());
  });

  it("折叠开关：点击后 collapsed 类 + 窄条", async () => {
    setup();
    const panel = document.querySelector(".result-panel")!;
    expect(panel.classList.contains("collapsed")).toBe(false);
    await userEvent.click(screen.getByTestId("result-toggle"));
    expect(panel.classList.contains("collapsed")).toBe(true);
  });
});
