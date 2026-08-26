import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FileTree, type FileTreeProps } from "./FileTree";
import type { FileEntry } from "../lib/fileTree";

/**
 * FileTree.test.tsx — 文件树 React 组件测试（Batch 3）。
 * 镜像 vanilla file-tree.js：懒加载目录树、展开/折叠、文件选中 + onOpenFile、
 * 根默认展开、加载失败提示。类名与 vanilla CSS 一致（Batch 5 复用）。
 */

const dir = (name: string, path: string): FileEntry => ({ name, path, type: "dir" });
const file = (name: string, path: string): FileEntry => ({ name, path, type: "file" });

function makeListFiles(childrenByPath: Record<string, FileEntry[]>) {
  return vi.fn(async (path: string) => ({ entries: childrenByPath[path] || [] }));
}

function setup(props: Partial<FileTreeProps> = {}) {
  const utils = render(<FileTree root="/proj" {...props} />);
  return { ...utils };
}

describe("FileTree", () => {
  it("根目录默认展开并懒加载子项（目录在前排序）", async () => {
    const listFiles = makeListFiles({
      "/proj": [file("b.txt", "/proj/b.txt"), dir("src", "/proj/src")],
    });
    setup({ listFiles });
    expect(listFiles).toHaveBeenCalledWith("/proj");
    await waitFor(() => {
      expect(screen.getByText("src")).toBeInTheDocument();
      expect(screen.getByText("b.txt")).toBeInTheDocument();
    });
    const rows = document.querySelectorAll(".ft-row");
    // 根行 + src（dir）+ b.txt（file）
    expect(rows[1]).toHaveClass("ft-dir");
    expect(rows[2]).toHaveClass("ft-file");
  });

  it("目录行点击 → 懒加载子项（一次拉取缓存）；折叠 → hidden", async () => {
    const listFiles = makeListFiles({
      "/proj": [dir("src", "/proj/src")],
      "/proj/src": [file("index.ts", "/proj/src/index.ts")],
    });
    setup({ listFiles });
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    const srcRow = screen.getByText("src").closest(".ft-row")!;
    await userEvent.click(srcRow);
    await waitFor(() => expect(screen.getByText("index.ts")).toBeInTheDocument());
    expect(listFiles).toHaveBeenCalledWith("/proj/src");
    // 折叠 → 子项隐藏（缓存保留，不重新拉取）
    await userEvent.click(srcRow);
    const kids = srcRow.querySelector(".ft-kids")!;
    expect(kids).toHaveClass("hidden");
    await userEvent.click(srcRow);
    await waitFor(() => expect(srcRow.querySelector(".ft-kids")!).not.toHaveClass("hidden"));
    expect(listFiles).toHaveBeenCalledTimes(2); // 仅 /proj + /proj/src，无重复拉取
  });

  it("文件行点击 → 选中 .active + onOpenFile(path)", async () => {
    const listFiles = makeListFiles({ "/proj": [file("a.py", "/proj/a.py")] });
    const onOpenFile = vi.fn();
    setup({ listFiles, onOpenFile });
    await waitFor(() => expect(screen.getByText("a.py")).toBeInTheDocument());
    const row = screen.getByText("a.py").closest(".ft-row")!;
    await userEvent.click(row);
    expect(row).toHaveClass("active");
    expect(onOpenFile).toHaveBeenCalledWith("/proj/a.py");
  });

  it("单选：切换选中只保留一个 .active", async () => {
    const listFiles = makeListFiles({
      "/proj": [file("a.py", "/proj/a.py"), file("b.py", "/proj/b.py")],
    });
    setup({ listFiles });
    await waitFor(() => expect(screen.getByText("a.py")).toBeInTheDocument());
    const rowA = screen.getByText("a.py").closest(".ft-row")!;
    const rowB = screen.getByText("b.py").closest(".ft-row")!;
    await userEvent.click(rowA);
    await userEvent.click(rowB);
    expect(rowA).not.toHaveClass("active");
    expect(rowB).toHaveClass("active");
  });

  it("加载失败 → 显示失败提示（error 态）", async () => {
    const listFiles = vi.fn(async () => {
      throw new Error("boom");
    });
    setup({ listFiles, t: (k) => (k === "result.treeLoadFailed" ? "LOAD_FAILED" : k) });
    await waitFor(() => expect(screen.getByText("LOAD_FAILED")).toBeInTheDocument());
  });

  it("嵌套目录加载失败 → 子目录行内显示失败提示（根成功，回归 #999 审查发现）", async () => {
    const listFiles = vi.fn(async (path: string) => {
      if (path === "/proj/src") throw new Error("boom");
      return { entries: [dir("src", "/proj/src")] };
    });
    setup({ listFiles, t: (k) => (k === "result.treeLoadFailed" ? "LOAD_FAILED" : k) });
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src").closest(".ft-row")!);
    // 修复前：子目录 catch 置 loaded:true + error:true，但提示条件要求
    // !st.loaded（永假）→ 失败静默；修复后与根错误路径一致（st.loaded && st.error）
    await waitFor(() => expect(screen.getByText("LOAD_FAILED")).toBeInTheDocument());
  });

  it("root 为空 → 显示 empty 提示（result.filesEmpty）", () => {
    setup({ root: "" });
    expect(screen.getByTestId("filetree-empty")).toHaveTextContent("No workspace");
  });

  it("根目录可折叠/展开（toggleDir 绑定，rant 2026-08-13T12:47:18）", async () => {
    const listFiles = makeListFiles({ "/proj": [file("x.txt", "/proj/x.txt")] });
    setup({ listFiles });
    await waitFor(() => expect(screen.getByText("x.txt")).toBeInTheDocument());
    const rootRow = document.querySelector(".ft-root")!;
    await userEvent.click(rootRow);
    expect(rootRow.querySelector(".ft-kids")).toHaveClass("hidden");
    await userEvent.click(rootRow);
    await waitFor(() => expect(rootRow.querySelector(".ft-kids")!).not.toHaveClass("hidden"));
  });

  it("行内缩进随深度递增（padding-left = 8 + depth*16）", async () => {
    const listFiles = makeListFiles({
      "/proj": [dir("src", "/proj/src")],
      "/proj/src": [file("deep.ts", "/proj/src/deep.ts")],
    });
    setup({ listFiles });
    await waitFor(() => expect(screen.getByText("src")).toBeInTheDocument());
    await userEvent.click(screen.getByText("src").closest(".ft-row")!);
    await waitFor(() => expect(screen.getByText("deep.ts")).toBeInTheDocument());
    const deepRow = screen.getByText("deep.ts").closest(".ft-row")!;
    expect((deepRow as HTMLElement).style.paddingLeft).toBe("40px"); // 8 + 2*16
  });
});
