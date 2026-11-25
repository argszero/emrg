import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { FileTree, FileTreeRoots } from "./FileTree";
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
  it("根是一行，不是工具条：根名占一行，刷新 / 全部收起没有了（rant 2026-11-20T21:27:29）", async () => {
    const load = tree([e("a.md")]);
    renderTree(load);
    const root = screen.getByTestId("t-root");
    expect(root).toHaveTextContent("r");
    // 根名从表头搬进了树：读屏也把它读成一行，行高与图标跟别的行同一套
    expect(root).toHaveAttribute("role", "treeitem");
    expect(root).toHaveClass("ft-line", "ft-row");
    expect(root).toHaveAttribute("data-depth", "0");
    expect(root.style.height).toBe(`${ROW_HEIGHT}px`);
    const glyph = root.querySelector(".ft-glyph") as HTMLElement;
    expect(glyph.style.width).toBe(`${ICON_PX}px`);
    // 那两个按钮连同装它们的工具条一起没了
    expect(screen.queryByTestId("t-refresh")).toBeNull();
    expect(screen.queryByTestId("t-collapse")).toBeNull();
    expect(screen.getByTestId("t").querySelectorAll("button, .ft-toolbar, .ft-tool")).toHaveLength(0);
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
    // 根自己占一层，所以第一级在 1、它的子级在 2（根还是表头时，第一级在 0）
    const root = screen.getByTestId("t-root");
    expect(root).toHaveAttribute("data-depth", "0");
    expect(rows[0]).toHaveAttribute("data-depth", "1");
    expect(rows[1]).toHaveAttribute("data-depth", "2");
    // 每一层一条 1px 引导线：行的深度就是引导线的条数，根行没有
    expect(rows[1].querySelectorAll("[data-testid='t-guide']")).toHaveLength(2);
    expect(rows[0].querySelectorAll("[data-testid='t-guide']")).toHaveLength(1);
    expect(root.querySelectorAll("[data-testid='t-guide']")).toHaveLength(0);
    // 每层缩进 8px：比的是层间步长，不是行首那点固定内边距
    const pad = (el: HTMLElement) => parseFloat(el.style.paddingLeft);
    expect(pad(rows[0] as HTMLElement) - pad(root)).toBe(INDENT_PX);
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

  it("键盘：↓ 移动选中，→ 展开，← 收起，点根行收起整棵树", async () => {
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
    // 根行自己一折就是整棵树收起——以前这是「全部收起」按钮的活
    const root = screen.getByTestId("t-root");
    fireEvent.click(root);
    expect(screen.queryAllByTestId("t-row")).toHaveLength(0);
    expect(root).toHaveAttribute("aria-expanded", "false");
    // 再点开，刚才那个打开着的文件夹还在：收起根不等于忘掉展开状态
    fireEvent.click(root);
    await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
  });

  /**
   * 右键菜单（issue #3041）。每一条对着工单的一项：右键先把该行选中且不改它的展开状态、
   * 两项的文案与顺序、复制的是绝对路径、写不进去时说的是失败、打开走已有的通道、根名作用于
   * 根目录而提示行什么都不出、键盘 ↑↓ / Enter / Esc、点外与滚动关闭、右下角向内翻转。
   */
  describe("右键菜单", () => {
    const MENU = "t-menu";

    function stubClipboard(writeText: (text: string) => Promise<void>): void {
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    }

    function stubOpenFile() {
      const openFile = vi.fn(async (_payload: { filePath: string }) => ({ ok: true }));
      (window as unknown as { emrg?: unknown }).emrg = { openFile };
      return openFile;
    }

    function unstub(): void {
      delete (window as unknown as { emrg?: unknown }).emrg;
      Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    }

    const dirTree = () =>
      vi.fn(async (p: string) =>
        p === "/r"
          ? { entries: [e("src", "dir"), e("a.md")], truncated: false }
          : { entries: [e("deep.md")], truncated: false },
      );

    it("右键一行：菜单出现、该行同时被选中，三项按固定顺序；目录不会因此展开", async () => {
      const load = dirTree();
      renderTree(load);
      await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
      const dirRow = screen.getAllByTestId("t-row")[0];

      fireEvent.contextMenu(dirRow, { clientX: 40, clientY: 60 });

      const menu = screen.getByTestId(MENU);
      expect(dirRow).toHaveAttribute("aria-selected", "true");
      const labels = Array.from(menu.querySelectorAll("[role='menuitem']")).map((n) => n.textContent);
      // 第三项「刷新」来自 issue #3226：这棵树只在根变化和首次展开时读盘，所以右键
      // 重读一层是它自己的入口。前两项的顺序不变，键盘索引因此也不用改。
      expect(labels).toEqual(["Copy full path", "Open with default app", "Refresh"]);
      // 展开是左键点目录的事：右键不该顺带把它打开，也不该去拉子项
      expect(dirRow).toHaveAttribute("aria-expanded", "false");
      expect(load).toHaveBeenCalledTimes(1);
    });

    it("复制：交给剪贴板的是这一行的绝对路径，成功后就地变成已复制并自行关闭", async () => {
      const writeText = vi.fn(async (_text: string) => {});
      stubClipboard(writeText);
      try {
        const load = dirTree();
        renderTree(load);
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
        fireEvent.click(screen.getAllByTestId("t-row")[0]);
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(3));
        // 展开后的顺序是：src、它自己的 deep.md、然后是文件 a.md
        const deep = screen.getAllByTestId("t-row")[1];
        expect(deep).toHaveAttribute("data-path", "/r/deep.md");

        fireEvent.contextMenu(deep, { clientX: 40, clientY: 60 });
        fireEvent.mouseDown(screen.getByTestId(`${MENU}-copy`));

        await waitFor(() => expect(writeText).toHaveBeenCalledWith("/r/deep.md"));
        await waitFor(() => expect(screen.getByTestId(`${MENU}-copy`)).toHaveTextContent("Copied ✓"));
        // 约一秒后菜单自行关闭
        await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull(), { timeout: 2500 });
      } finally {
        unstub();
      }
    });

    it("复制失败：就地变成复制失败，菜单留着", async () => {
      stubClipboard(() => Promise.reject(new Error("clipboard denied")));
      try {
        renderTree(tree([e("a.md")]));
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
        fireEvent.contextMenu(screen.getAllByTestId("t-row")[0], { clientX: 40, clientY: 60 });
        fireEvent.mouseDown(screen.getByTestId(`${MENU}-copy`));
        await waitFor(() => expect(screen.getByTestId(`${MENU}-copy`)).toHaveTextContent("Copy failed"));
        expect(screen.getByTestId(MENU)).toBeInTheDocument();
      } finally {
        unstub();
      }
    });

    it("使用默认方式打开：把这一行的路径交给打开通道，菜单随即关掉", async () => {
      const openFile = stubOpenFile();
      try {
        renderTree(tree([e("a.md")]));
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
        fireEvent.contextMenu(screen.getAllByTestId("t-row")[0], { clientX: 40, clientY: 60 });
        fireEvent.mouseDown(screen.getByTestId(`${MENU}-open`));
        await waitFor(() => expect(openFile).toHaveBeenCalledWith({ filePath: "/r/a.md" }));
        await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull());
      } finally {
        unstub();
      }
    });

    it("根名右键作用于根目录；空、加载中、读不了的提示行右键都不出菜单", async () => {
      const writeText = vi.fn(async (_text: string) => {});
      stubClipboard(writeText);
      try {
        const first = renderTree(tree([e("a.md")]));
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
        fireEvent.contextMenu(screen.getByTestId("t-root"), { clientX: 10, clientY: 10 });
        fireEvent.mouseDown(screen.getByTestId(`${MENU}-copy`));
        await waitFor(() => expect(writeText).toHaveBeenCalledWith("/r"));
        first.unmount();

        // 空目录
        const empty = renderTree(tree([]));
        await waitFor(() => expect(screen.getByTestId("t-empty")).toBeInTheDocument());
        fireEvent.contextMenu(screen.getByTestId("t-empty"));
        expect(screen.queryByTestId(MENU)).toBeNull();
        empty.unmount();

        // 加载中
        const pending = renderTree(vi.fn(() => new Promise(() => {})) as never);
        expect(screen.getByTestId("t-loading")).toBeInTheDocument();
        fireEvent.contextMenu(screen.getByTestId("t-loading"));
        expect(screen.queryByTestId(MENU)).toBeNull();
        pending.unmount();

        // 读不了
        renderTree(
          vi.fn(async () => {
            throw new Error("path not found: /r");
          }),
        );
        await waitFor(() => expect(screen.getByTestId("t-error")).toBeInTheDocument());
        fireEvent.contextMenu(screen.getByTestId("t-error"));
        expect(screen.queryByTestId(MENU)).toBeNull();
      } finally {
        unstub();
      }
    });

    it("键盘：↓↑ 移动菜单高亮而不是行选中，Enter 执行，Esc 关闭", async () => {
      const writeText = vi.fn(async (_text: string) => {});
      stubClipboard(writeText);
      try {
        renderTree(dirTree());
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(2));
        const body = screen.getByTestId("t-body");
        const fileRow = screen.getAllByTestId("t-row")[1];
        fireEvent.contextMenu(fileRow, { clientX: 40, clientY: 60 });

        expect(screen.getByTestId(`${MENU}-copy`)).toHaveClass("active");
        fireEvent.keyDown(body, { key: "ArrowDown" });
        await waitFor(() => expect(screen.getByTestId(`${MENU}-open`)).toHaveClass("active"));
        // 菜单开着时箭头不再移动行选中
        expect(fileRow).toHaveAttribute("aria-selected", "true");
        fireEvent.keyDown(body, { key: "ArrowUp" });
        await waitFor(() => expect(screen.getByTestId(`${MENU}-copy`)).toHaveClass("active"));

        fireEvent.keyDown(body, { key: "Enter" });
        await waitFor(() => expect(writeText).toHaveBeenCalledWith("/r/a.md"));

        fireEvent.keyDown(body, { key: "Escape" });
        await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull());
      } finally {
        unstub();
      }
    });

    it("点菜单外、滚动、换根都关掉菜单", async () => {
      const view = renderTree(tree([e("a.md")]));
      await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
      const row = screen.getAllByTestId("t-row")[0];

      fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
      expect(screen.getByTestId(MENU)).toBeInTheDocument();
      fireEvent.mouseDown(document.body);
      await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull());

      fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
      fireEvent.scroll(screen.getByTestId("t-body"));
      await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull());

      fireEvent.contextMenu(row, { clientX: 40, clientY: 60 });
      view.rerender(
        <I18nProvider lang="en">
          <FileTree testId="t" root="/other" loadDir={tree([e("b.md")])} />
        </I18nProvider>,
      );
      await waitFor(() => expect(screen.queryByTestId(MENU)).toBeNull());
    });

    it("贴着窗口右下角时向内翻转，两条边都不出窗口；在窗口中间不翻", async () => {
      const rect = { width: 180, height: 72, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) };
      const rectSpy = vi
        .spyOn(HTMLElement.prototype, "getBoundingClientRect")
        .mockReturnValue(rect as DOMRect);
      const before = { w: window.innerWidth, h: window.innerHeight };
      Object.defineProperty(window, "innerWidth", { value: 800, configurable: true });
      Object.defineProperty(window, "innerHeight", { value: 600, configurable: true });
      try {
        renderTree(tree([e("a.md")]));
        await waitFor(() => expect(screen.getAllByTestId("t-row")).toHaveLength(1));
        const row = screen.getAllByTestId("t-row")[0];

        fireEvent.contextMenu(row, { clientX: 400, clientY: 300 });
        const inside = screen.getByTestId(MENU);
        expect(inside).toHaveAttribute("data-flipped-x", "0");
        expect(inside).toHaveAttribute("data-flipped-y", "0");

        fireEvent.contextMenu(row, { clientX: 795, clientY: 595 });
        const menu = screen.getByTestId(MENU) as HTMLElement;
        expect(menu).toHaveAttribute("data-flipped-x", "1");
        expect(menu).toHaveAttribute("data-flipped-y", "1");
        const left = parseFloat(menu.style.left);
        const top = parseFloat(menu.style.top);
        expect(left).toBe(795 - rect.width);
        expect(top).toBe(595 - rect.height);
        expect(left + rect.width).toBeLessThanOrEqual(800);
        expect(top + rect.height).toBeLessThanOrEqual(600);
      } finally {
        rectSpy.mockRestore();
        Object.defineProperty(window, "innerWidth", { value: before.w, configurable: true });
        Object.defineProperty(window, "innerHeight", { value: before.h, configurable: true });
      }
    });
  });
});

describe("FileTreeRoots", () => {
  const ent = (path: string, name: string, type: "dir" | "file" = "file"): TreeEntry => ({ path, name, type });

  const rootsLoad = () =>
    vi.fn(async (p: string) => {
      if (p === "/s") return { entries: [ent("/s/sub", "sub", "dir"), ent("/s/notes.md", "notes.md")], truncated: false };
      if (p === "/s/sub") return { entries: [ent("/s/sub/leaf.md", "leaf.md")], truncated: false };
      if (p === "/x") return { entries: [ent("/x/deep", "deep", "dir")], truncated: false };
      if (p === "/x/deep") return { entries: [ent("/x/deep/xleaf.md", "xleaf.md")], truncated: false };
      return { entries: [], truncated: false };
    });

  it("会话目录第一行，额外可写根依次在下，各自独立展开", async () => {
    const load = rootsLoad();
    render(
      <I18nProvider lang="en">
        <FileTreeRoots testId="s" roots={["/s", "/x"]} loadDir={load} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("s-roots")).toHaveAttribute("data-root-count", "2");
    expect(screen.getByTestId("s-root")).toHaveTextContent("s");
    expect(screen.getByTestId("s-extra-1-root")).toHaveTextContent("x");
    await waitFor(() => expect(screen.getAllByTestId("s-row")).toHaveLength(2));
    await waitFor(() => expect(screen.getAllByTestId("s-extra-1-row")).toHaveLength(1));

    // 展开额外根下的目录只影响那一棵树
    fireEvent.click(screen.getAllByTestId("s-extra-1-row")[0]);
    await waitFor(() => expect(screen.getAllByTestId("s-extra-1-row")).toHaveLength(2));
    expect(screen.getAllByTestId("s-row")).toHaveLength(2);
  });

  it("调用方给的层数转发给每一棵树：两个根都从那一层开始（rant 2026-11-20T21:27:29）", async () => {
    render(
      <I18nProvider lang="en">
        <FileTreeRoots testId="s" roots={["/s", "/x"]} loadDir={rootsLoad()} depthOffset={1} />
      </I18nProvider>,
    );
    await waitFor(() => expect(screen.getAllByTestId("s-row")).toHaveLength(2));
    // 两个根都在第 1 层、行都在第 2 层：少转发一次，第二个根就会爬回第 0 层
    expect(screen.getByTestId("s-root")).toHaveAttribute("data-depth", "1");
    expect(screen.getByTestId("s-extra-1-root")).toHaveAttribute("data-depth", "1");
    expect(screen.getAllByTestId("s-row")[0]).toHaveAttribute("data-depth", "2");
    expect(screen.getAllByTestId("s-extra-1-row")[0]).toHaveAttribute("data-depth", "2");
  });

  it("同一路径只画一次，尾部分隔符是同一个路径", () => {
    render(
      <I18nProvider lang="en">
        <FileTreeRoots testId="s" roots={["/s", "/s/", "/x"]} loadDir={rootsLoad()} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("s-roots")).toHaveAttribute("data-root-count", "2");
    expect(screen.getByTestId("s-extra-1-root")).toHaveTextContent("x");
    expect(screen.queryByTestId("s-extra-2-root")).toBeNull();
  });

  it("读不出的根把原因写在自己下面，另一棵树照常显示", async () => {
    const load = vi.fn(async (p: string) => {
      if (p === "/s") return { entries: [ent("/s/notes.md", "notes.md")], truncated: false };
      throw new Error("no such directory");
    });
    render(
      <I18nProvider lang="en">
        <FileTreeRoots testId="s" roots={["/s", "/gone"]} loadDir={load} />
      </I18nProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("s-extra-1-error")).toHaveTextContent("no such directory"));
    await waitFor(() => expect(screen.getAllByTestId("s-row")).toHaveLength(1));
  });

  it("没有额外根时就是原来那一棵树，不多出壳", () => {
    render(
      <I18nProvider lang="en">
        <FileTreeRoots testId="s" roots={["/s"]} loadDir={rootsLoad()} />
      </I18nProvider>,
    );
    expect(screen.queryByTestId("s-roots")).toBeNull();
    expect(screen.getByTestId("s-root")).toBeInTheDocument();
  });

  it("调用方交来的展开集合决定哪些目录是开的，并把它们的下一层重新读出来", async () => {
    const load = rootsLoad();
    render(
      <I18nProvider lang="en">
        <FileTreeRoots
          testId="s"
          roots={["/s"]}
          loadDir={load}
          expanded={new Set(["/s/sub"])}
          onExpandedChange={() => {}}
        />
      </I18nProvider>,
    );
    await waitFor(() => expect(screen.getAllByTestId("s-row")).toHaveLength(3));
    const names = screen.getAllByTestId("s-row").map((r) => r.textContent);
    expect(names[1]).toContain("leaf.md");
    expect(load).toHaveBeenCalledWith("/s/sub");
  });
});

describe("刷新（issue #3226）", () => {
  /** 一层目录：根下有一个子目录，子目录里有一个文件。 */
  const subTree = () =>
    vi.fn(async (p: string) => ({
      entries:
        p === "/r"
          ? [{ name: "sub", path: "/r/sub", type: "dir" as const }]
          : [{ name: "inner.md", path: "/r/inner.md", type: "file" as const }],
      truncated: false,
    }));

  const treeWith = (load: ReturnType<typeof subTree>, key: number) => (
    <I18nProvider lang="en">
      <FileTree testId="t" root="/r" loadDir={load} refreshKey={key} />
    </I18nProvider>
  );

  it("面板刷新：根与已展开的那一层都重读，展开状态不重来", async () => {
    const load = subTree();
    const { rerender } = render(treeWith(load, 0));
    await waitFor(() => expect(screen.getByText("sub")).toBeInTheDocument());
    fireEvent.click(screen.getAllByTestId("t-row")[0]);
    await waitFor(() => expect(screen.getByText("inner.md")).toBeInTheDocument());
    const before = load.mock.calls.length;
    // 值没变就不是新事件：同一次刷新被读第二遍，不该再打一次盘
    rerender(treeWith(load, 0));
    expect(load.mock.calls.length).toBe(before);
    rerender(treeWith(load, 1));
    await waitFor(() => expect(load.mock.calls.length).toBeGreaterThan(before));
    expect(load).toHaveBeenCalledWith("/r");
    expect(load).toHaveBeenCalledWith("/r/sub");
    // 没有清空重画：展开的那一层还画着
    expect(screen.getByText("inner.md")).toBeInTheDocument();
  });

  it("右键「刷新」只重读被点的那一层", async () => {
    const load = subTree();
    render(treeWith(load, 0));
    await waitFor(() => expect(screen.getByText("sub")).toBeInTheDocument());
    fireEvent.click(screen.getAllByTestId("t-row")[0]);
    await waitFor(() => expect(screen.getByText("inner.md")).toBeInTheDocument());
    const before = load.mock.calls.length;
    fireEvent.contextMenu(screen.getAllByTestId("t-row")[0], { clientX: 40, clientY: 60 });
    // 菜单项在 mouseDown 上执行，不是 click：点下去的瞬间菜单就关了，click 等不到
    fireEvent.mouseDown(screen.getByTestId("t-menu-refresh"));
    await waitFor(() => expect(load.mock.calls.length).toBe(before + 1));
    expect(load).toHaveBeenLastCalledWith("/r/sub");
  });
});
