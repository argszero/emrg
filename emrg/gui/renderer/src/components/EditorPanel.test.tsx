import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { EditorPanel, type EditorPanelHandle } from "./EditorPanel";
import { LeftTrees } from "./LeftTrees";

/**
 * EditorPanel — the right-hand column as an editor (issue #3068).
 *
 * One test per acceptance item the issue names. The tabs are opened through the same
 * handle the session column uses (`openFile`), so what is measured here is the path a
 * real click takes rather than a prop a test could hand itself.
 */

const FILES: Record<string, string> = {
  "/p/emrg/a.txt": "alpha\nbeta\ngamma",
  "/p/emrg/b.txt": "one\ntwo",
  "/p/emrg/sub/c.txt": "c",
};

function renderPanel(onOpenMemory = vi.fn()) {
  const ref = createRef<EditorPanelHandle>();
  const loadFile = vi.fn(async (p: string) => ({
    content: FILES[p] ?? "",
    binary: false,
    truncated: false,
    totalLines: null,
  }));
  render(
    <I18nProvider lang="en">
      <EditorPanel ref={ref} loadFile={loadFile} onOpenMemory={onOpenMemory} />
    </I18nProvider>,
  );
  const open = (path: string, root = "/p/emrg", preview = true) => {
    act(() => ref.current?.openFile({ path, root, preview }));
  };
  return { ref, loadFile, open, onOpenMemory };
}

describe("EditorPanel tabs (issue #3068)", () => {
  it("标签栏第一个永远是「记忆」，没有关闭按钮；右栏不再有 agent 目录 / 会话目录两个标签", () => {
    renderPanel();
    const strip = screen.getByTestId("editor-tabs");
    const memory = screen.getByTestId("editor-tab-memory");
    expect(strip.firstElementChild).toBe(memory);
    expect(memory).toHaveTextContent("Memory");
    expect(within(memory).queryByTestId("editor-tab-close")).toBeNull();
    expect(screen.queryByText("Agent dir")).toBeNull();
    expect(screen.queryByText("Session dir")).toBeNull();
  });

  it("点一个文件：多一个标签、它被选中、面包屑等于完整路径；再点同一个文件标签数不变", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    expect(screen.getAllByTestId("editor-tab")).toHaveLength(1);
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("editor-breadcrumb")).toHaveTextContent("/p/emrg/a.txt");
    await screen.findByText(/alpha/);
    open("/p/emrg/a.txt");
    expect(screen.getAllByTestId("editor-tab")).toHaveLength(1);
  });

  it("单击是预览：再单击另一个文件，预览那个被顶掉；双击固定，两个都在", () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    open("/p/emrg/b.txt");
    const one = screen.getAllByTestId("editor-tab");
    expect(one).toHaveLength(1);
    expect(one[0]).toHaveAttribute("data-path", "/p/emrg/b.txt");
    // 预览的标签名字是斜体
    expect(screen.getByTestId("editor-tab-label").style.fontStyle).toBe("italic");

    open("/p/emrg/a.txt", "/p/emrg", false); // 双击 = 固定
    open("/p/emrg/b.txt");
    expect(screen.getAllByTestId("editor-tab")).toHaveLength(2);
    // 固定下来的那个不再是预览
    const pinned = screen.getAllByTestId("editor-tab").find((el) => el.getAttribute("data-path") === "/p/emrg/a.txt");
    expect(pinned).toHaveAttribute("data-preview", "false");
  });

  it("在标签上双击也把它固定下来", () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    const tab = screen.getByTestId("editor-tab");
    expect(tab).toHaveAttribute("data-preview", "true");
    fireEvent.doubleClick(tab);
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-preview", "false");
  });

  it("同一个文件已经在标签里时只被选中：再点一次不新开、也不把它降级成预览", () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false); // 固定
    open("/p/emrg/b.txt", "/p/emrg", false);
    open("/p/emrg/a.txt"); // 单击一个已经打开（且固定）的文件
    const tabs = screen.getAllByTestId("editor-tab");
    expect(tabs).toHaveLength(2);
    const a = tabs.find((el) => el.getAttribute("data-path") === "/p/emrg/a.txt");
    expect(a).toHaveAttribute("aria-selected", "true");
    // 它已经固定了，单击只是选中它
    expect(a).toHaveAttribute("data-preview", "false");
  });

  it("✕ 关掉当前标签后右邻接手；逐个关光只剩「记忆」且门可见、代码区与面包屑不显示", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    open("/p/emrg/b.txt", "/p/emrg", false);
    open("/p/emrg/sub/c.txt", "/p/emrg", false);
    // c is the tab that is active now, so that is the text the read must deliver.
    await screen.findByTestId("editor-code");
    // 激活中间那个，关掉它 → 右边那个（c）接手
    fireEvent.click(screen.getAllByTestId("editor-tab")[1]);
    expect(screen.getAllByTestId("editor-tab")[1]).toHaveAttribute("aria-selected", "true");
    fireEvent.click(within(screen.getAllByTestId("editor-tab")[1]).getByTestId("editor-tab-close"));
    const left = screen.getAllByTestId("editor-tab");
    expect(left).toHaveLength(2);
    expect(left[1]).toHaveAttribute("aria-selected", "true");
    expect(left[1]).toHaveAttribute("data-path", "/p/emrg/sub/c.txt");
    // 关光
    for (const tab of [...screen.getAllByTestId("editor-tab")]) {
      fireEvent.click(within(tab).getByTestId("editor-tab-close"));
    }
    expect(screen.queryAllByTestId("editor-tab")).toHaveLength(0);
    expect(screen.getByTestId("editor-tab-memory")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("editor-memory-door")).toBeInTheDocument();
    expect(screen.queryByTestId("editor-code")).toBeNull();
    expect(screen.queryByTestId("editor-breadcrumb")).toBeNull();
  });

  it("中键点标签也能关", () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    fireEvent.mouseDown(screen.getByTestId("editor-tab"), { button: 1 });
    expect(screen.queryAllByTestId("editor-tab")).toHaveLength(0);
  });

  it("每个标签自己记住滚动位置：A 滚过之后去 B 再回来，位置不变", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await screen.findByText(/alpha/);
    const code = screen.getByTestId("editor-code");
    code.scrollTop = 120;
    fireEvent.scroll(code);
    open("/p/emrg/b.txt", "/p/emrg", false);
    await screen.findByText(/one/);
    expect(screen.getByTestId("editor-code").scrollTop).not.toBe(120);
    fireEvent.click(screen.getAllByTestId("editor-tab")[0]);
    expect(screen.getByTestId("editor-code").scrollTop).toBe(120);
  });

  it("标签条在自己宽度内横向滚，不换行、不撑宽面板，也不引起整页横滚", () => {
    const { open } = renderPanel();
    for (let i = 0; i < 12; i += 1) open(`/p/emrg/f${i}.txt`, "/p/emrg", false);
    expect(screen.getAllByTestId("editor-tab")).toHaveLength(12);
    const strip = screen.getByTestId("editor-tabs");
    expect(strip.style.flexWrap).toBe("nowrap");
    expect(strip.style.overflowX).toBe("auto");
    expect(strip.style.minWidth).toBe("0px");
    // 每个标签都不许缩、不许撑：标签再多，面板也只由右栏宽度决定
    for (const tab of screen.getAllByTestId("editor-tab")) {
      expect(tab.style.flex).toBe("0 0 auto");
    }
    expect(screen.getByTestId("editor-panel").style.width).toBe("");
    // 竖着滚的滚轮把标签条往横向推
    strip.scrollLeft = 0;
    fireEvent.wheel(strip, { deltaY: 90 });
    expect(strip.scrollLeft).toBe(90);
  });

  it("行号是固定列，横向滚动时钉在左边", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    await screen.findByText(/alpha/);
    const gutter = screen.getByTestId("editor-gutter");
    expect(gutter.style.position).toBe("sticky");
    expect(gutter.style.left).toBe("0px");
    // 行号与正文同在一个滚动容器里，所以两者一起纵向滚
    expect(screen.getByTestId("editor-code").contains(gutter)).toBe(true);
    expect(gutter.textContent).toBe("123");
  });

  it("记忆门的按钮仍然打开记忆面板", () => {
    const { onOpenMemory } = renderPanel();
    fireEvent.click(screen.getByTestId("editor-memory-open"));
    expect(onOpenMemory).toHaveBeenCalledTimes(1);
  });
});

describe("LeftTrees → editor (issue #3068)", () => {
  const entries = [
    { name: "a.txt", path: "/p/emrg/a.txt", type: "file" as const },
    { name: "sub", path: "/p/emrg/sub", type: "dir" as const },
  ];
  const loadDir = vi.fn(async () => ({ entries, truncated: false }));

  const renderTrees = () => {
    const onOpenFile = vi.fn();
    render(
      <I18nProvider lang="en">
        <LeftTrees agentRoot="/p/emrg" sessionRoot="/p/emrg/.emrg/sessions/s1" loadDir={loadDir} onOpenFile={onOpenFile} />
      </I18nProvider>,
    );
    return { onOpenFile };
  };

  it("点文件行交上绝对路径与所在树；点目录行什么都不交", async () => {
    const { onOpenFile } = renderTrees();
    const row = await screen.findAllByTestId("left-tree-agent-row");
    const file = row.find((el) => el.getAttribute("data-kind") === "text");
    const dir = row.find((el) => el.getAttribute("data-kind") !== "text");
    expect(file).toBeDefined();
    fireEvent.click(file as HTMLElement);
    expect(onOpenFile).toHaveBeenCalledWith({ path: "/p/emrg/a.txt", root: "/p/emrg", pin: false });
    onOpenFile.mockClear();
    fireEvent.click(dir as HTMLElement);
    expect(onOpenFile).not.toHaveBeenCalled();
  });

  it("双击文件行是固定", async () => {
    const { onOpenFile } = renderTrees();
    const row = await screen.findAllByTestId("left-tree-agent-row");
    const file = row.find((el) => el.getAttribute("data-kind") === "text");
    fireEvent.doubleClick(file as HTMLElement);
    expect(onOpenFile).toHaveBeenCalledWith({ path: "/p/emrg/a.txt", root: "/p/emrg", pin: true });
  });
});
