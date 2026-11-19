import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { FileTree } from "./FileTree";
import { ICON_PX, INDENT_PX, ROW_HEIGHT, type TreeEntry } from "../lib/fileTree";

/**
 * FileTree.test.tsx — the inspector's two directory trees, held to the issue's items
 * (issue #3026).
 *
 * Every assertion here is one numbered requirement: what the toolbar carries, which
 * entries an open folder contributes and at what depth, that a listing cut short says
 * so, and that an unreadable path reports its reason instead of drawing an empty
 * directory. The visual numbers are read off the rendered rows rather than trusted to
 * the stylesheet, which is what makes the height/indent mutations fail.
 */

const e = (name: string, type: "dir" | "file" = "file"): TreeEntry => ({
  name,
  path: `/r/${name}`,
  type,
});

const tree = (entries: TreeEntry[], truncated = false) =>
  vi.fn(async (_p: string) => ({ entries, truncated }));

function renderTree(loadDir: ReturnType<typeof tree>, root: string | null = "/r") {
  return render(
    <I18nProvider lang="en">
      <FileTree testId="t" root={root} loadDir={loadDir} />
    </I18nProvider>,
  );
}

describe("FileTree", () => {
  it("顶部一行根名 + 刷新 / 全部收起两个按钮", async () => {
    const load = tree([e("a.md")]);
    renderTree(load);
    expect(screen.getByTestId("t-root")).toHaveTextContent("r");
    expect(screen.getByTestId("t-refresh")).toBeInTheDocument();
    expect(screen.getByTestId("t-collapse")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
  });

  it("目录在前、文件在后，点开头的条目照常显示", async () => {
    const load = tree([e("b.md"), e("src", "dir"), e(".emrg", "dir"), e(".gitignore")]);
    renderTree(load);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(4));
    const names = screen.getAllByTestId("t-row").map((r) => r.textContent);
    expect(names[0]).toContain(".emrg");
    expect(names[1]).toContain("src");
    expect(names[2]).toContain(".gitignore");
    expect(names[3]).toContain("b.md");
  });

  it("逐层懒加载：点开文件夹才拉它的子项，子项缩进一层且带引导线", async () => {
    const load = vi.fn(async (p: string) =>
      p === "/r"
        ? { entries: [e("src", "dir")], truncated: false }
        : { entries: [e("deep.md")], truncated: false },
    );
    renderTree(load);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
    // 根这一层只拉过一次，子级一次都没拉
    expect(load).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getAllByTestId("t-row")[0]);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
    expect(load).toHaveBeenCalledWith("/r/src");

    const rows = screen.getAllByTestId("t-row");
    expect(rows[1]).toHaveAttribute("data-depth", "1");
    // 子行缩进一层，并有一条 1px 引导线；根行两者都没有
    expect(rows[1].querySelectorAll("[data-testid='t-guide']")).toHaveLength(1);
    expect(rows[0].querySelectorAll("[data-testid='t-guide']")).toHaveLength(0);
    // 每层缩进 8px：比的是层间步长，不是行首那点固定内边距
    const pad = (el: HTMLElement) => parseFloat(el.style.paddingLeft);
    expect(pad(rows[1] as HTMLElement) - pad(rows[0] as HTMLElement)).toBe(INDENT_PX);
  });

  it("视觉常量写在行上：行高 22、图标 16", async () => {
    const load = tree([e("a.md")]);
    renderTree(load);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
    const row = screen.getAllByTestId("t-row")[0];
    expect(row.style.height).toBe(`${ROW_HEIGHT}px`);
    expect(ROW_HEIGHT).toBe(22);
    expect(ICON_PX).toBe(16);
    const glyph = row.querySelector(".ft-glyph") as HTMLElement;
    expect(glyph.style.width).toBe("16px");
    expect(row).toHaveAttribute("data-kind", "markdown");
  });

  it("空目录明说空，读不到就给出原因而不是一棵空树", async () => {
    const empty = tree([]);
    const first = renderTree(empty);
    await waitFor(() => expect(screen.getByTestId("t-empty")).toHaveTextContent("empty"));
    first.unmount();

    const failing = vi.fn(async () => {
      throw new Error("path not found: /r");
    });
    renderTree(failing);
    await waitFor(() => expect(screen.getByTestId("t-error")).toHaveTextContent("path not found"));
    expect(screen.queryAllByTestId("t-row")).toHaveLength(0);
  });

  it("条目被截断时说还有更多", async () => {
    const load = tree([e("a.md")], true);
    renderTree(load);
    await waitFor(() => expect(screen.getByTestId("t-more")).toBeInTheDocument());
  });

  it("这一版没有读目录的通道时，说的是通道缺失而不是目录为空", async () => {
    render(
      <I18nProvider lang="en">
        <FileTree testId="t" root="/r" />
      </I18nProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("t-error")).toHaveTextContent("cannot list directories"));
  });

  it("键盘：↓ 移动选中，→ 展开，← 收起，全部收起回到一行", async () => {
    const load = vi.fn(async (p: string) =>
      p === "/r"
        ? { entries: [e("src", "dir")], truncated: false }
        : { entries: [e("deep.md")], truncated: false },
    );
    renderTree(load);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
    const body = screen.getByTestId("t-body");

    fireEvent.keyDown(body, { key: "ArrowDown" });
    await waitFor(() =>
      expect(screen.getAllByTestId("t-row")[0]).toHaveAttribute("aria-selected", "true"),
    );

    fireEvent.keyDown(body, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));

    fireEvent.keyDown(body, { key: "ArrowLeft" });
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));

    fireEvent.click(screen.getAllByTestId("t-row")[0]);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
    fireEvent.click(screen.getByTestId("t-collapse"));
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
  });
});
