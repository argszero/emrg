import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { EditorSelection, EditorState } from "@codemirror/state";
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
  "/p/emrg/a.py": "alpha\nbeta\ngamma",
  "/p/emrg/notes.zzz": "plain words",
};

interface PanelOptions {
  sid?: string | null;
  /** What the reader answers about one path: a binary frame, or a window instead of a file. */
  shape?: Record<string, { binary?: boolean; truncated?: boolean }>;
  saveFile?: (
    path: string,
    content: string,
    sid: string,
  ) => Promise<{ mtime: number; size: number } | null>;
  statFile?: (
    path: string,
  ) => Promise<{ exists: boolean; mtime: number | null; size: number | null } | null>;
}

function renderPanel(options: PanelOptions = {}) {
  const ref = createRef<EditorPanelHandle>();
  const loadFile = vi.fn(async (p: string) => ({
    content: FILES[p] ?? "",
    binary: options.shape?.[p]?.binary ?? false,
    truncated: options.shape?.[p]?.truncated ?? false,
    totalLines: null,
  }));
  const saveFile = options.saveFile ?? vi.fn(async () => ({ mtime: 200, size: 20 }));
  const statFile =
    options.statFile ?? vi.fn(async () => ({ exists: true, mtime: 100, size: 10 }));
  render(
    <I18nProvider lang="en">
      <EditorPanel
        ref={ref}
        sid={options.sid ?? "s1"}
        loadFile={loadFile}
        saveFile={saveFile}
        statFile={statFile}
      />
    </I18nProvider>,
  );
  const open = (path: string, root = "/p/emrg", preview = true) => {
    act(() => ref.current?.openFile({ path, root, preview }));
  };
  return { ref, loadFile, saveFile, statFile, open };
}

/** The editor's own root, and the live CodeMirror view inside it. */
function surface(): HTMLElement {
  return screen.getByTestId("editor-surface");
}

function editor(): EditorView {
  const dom = surface().querySelector(".cm-editor") as HTMLElement | null;
  const found = dom ? EditorView.findFromDOM(dom) : null;
  if (!found) throw new Error("the panel did not mount an editor");
  return found;
}

/** Type at the end of the buffer, the way a keystroke reaches the document. */
function typeInto(text: string): void {
  act(() => {
    const view = editor();
    view.dispatch({ changes: { from: view.state.doc.length, insert: text } });
  });
}

/** `Mod-s`, sent with the modifier CodeMirror resolves `Mod` to under jsdom. */
function pressSave(): void {
  fireEvent.keyDown(editor().contentDOM, { key: "s", ctrlKey: true });
}

describe("EditorPanel tabs (issue #3068)", () => {
  it("标签栏里没有「记忆」标签，也没有 agent 目录 / 会话目录两个标签", () => {
    renderPanel();
    const strip = screen.getByTestId("editor-tabs");
    // 一个文件都没打开 → 标签栏里没有任何标签，第一个也不是「记忆」
    expect(strip.childElementCount).toBe(0);
    expect(screen.queryByTestId("editor-tab-memory")).toBeNull();
    expect(screen.queryByText("Agent dir")).toBeNull();
    expect(screen.queryByText("Session dir")).toBeNull();
  });

  it("一个文件都没打开时右栏什么都不渲染——不写提示语，不给替代按钮", () => {
    renderPanel();
    const panel = screen.getByTestId("editor-panel");
    // 空栏：面板自己的可见文本一个字符都没有，按钮一个都没有
    expect(panel.textContent).toBe("");
    expect(panel.querySelectorAll("button")).toHaveLength(0);
    expect(screen.queryByTestId("editor-code")).toBeNull();
    expect(screen.queryByTestId("editor-breadcrumb")).toBeNull();
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

  it("✕ 关掉当前标签后右邻接手；逐个关光后右栏空着，代码区与面包屑不显示", async () => {
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
    // 关光最后一个之后回到「没有打开的文件」，右栏空着
    expect(screen.getByTestId("editor-tabs").childElementCount).toBe(0);
    expect(screen.getByTestId("editor-panel").textContent).toBe("");
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
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    // The scroller is CodeMirror's own `.cm-scroller`: the panel's wrapper no longer holds
    // the offset. It is also a **new** element after a tab switch — an unread tab has no
    // editor for one render — so each reading re-queries it instead of holding the node.
    const scroller = (): HTMLElement =>
      surface().querySelector(".cm-scroller") as HTMLElement;
    scroller().scrollTop = 120;
    fireEvent.scroll(scroller());
    open("/p/emrg/b.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("one"));
    expect(scroller().scrollTop).toBe(0);
    fireEvent.click(screen.getAllByTestId("editor-tab")[0]);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    expect(scroller().scrollTop).toBe(120);
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

  it("行号是编辑器自己的固定列：与正文同一个滚动容器，横向滚动时钉在左边", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt");
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    // 行号不再由面板手写：CodeMirror 的 `.cm-gutters` 就是它自己的固定列，钉在滚动容器
    // 第一列。面板要做的是不把它拿掉，也不另造一套行号。
    const scroller = surface().querySelector(".cm-scroller");
    const gutters = surface().querySelector(".cm-gutters");
    expect(gutters).not.toBeNull();
    expect(scroller?.contains(gutters as Node)).toBe(true);
    expect(gutters?.textContent).toContain("1");
    expect(gutters?.textContent).toContain("3");
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
    // agent 目录默认收起（rant 2026-11-20T17:56:07 清单 2）：先展开，它下面的行才存在
    fireEvent.click(screen.getByTestId("left-group-agent-row"));
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
    // agent 目录默认收起（rant 2026-11-20T17:56:07 清单 2）：先展开，它下面的行才存在
    fireEvent.click(screen.getByTestId("left-group-agent-row"));
    const row = await screen.findAllByTestId("left-tree-agent-row");
    const file = row.find((el) => el.getAttribute("data-kind") === "text");
    fireEvent.doubleClick(file as HTMLElement);
    expect(onOpenFile).toHaveBeenCalledWith({ path: "/p/emrg/a.txt", root: "/p/emrg", pin: true });
  });
});

/**
 * The right column as an editor (rant 2026-11-20T18:01:52).
 *
 * Each test drives the surface the host drives: a keystroke becomes a CodeMirror
 * transaction, a save is `Mod-s`, a close is the tab's own button. Nothing here hands the
 * panel a buffer directly — a test that did would pass on a panel that never wires the
 * editor to the save path, which is the defect this work exists to remove.
 */
describe("EditorPanel as an editor (rant 2026-11-20T18:01:52)", () => {
  it("打个字就有脏点，保存把它写回原文件，脏点随之消失", async () => {
    const { open, saveFile } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "false");
    expect(screen.queryByTestId("editor-tab-dirty")).toBeNull();

    typeInto("X");
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");
    expect(screen.getByTestId("editor-tab-dirty")).toBeInTheDocument();

    pressSave();
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    // 写的是改完的全文、写回的是这个文件、会话 id 一起交上去
    expect(saveFile).toHaveBeenCalledWith("/p/emrg/a.txt", "alpha\nbeta\ngammaX", "s1");
    await waitFor(() =>
      expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "false"),
    );
    expect(screen.queryByTestId("editor-tab-dirty")).toBeNull();
  });

  it("保存失败：脏点留着，提示看得见，而且写着失败的原因", async () => {
    const saveFile = vi.fn(async () => {
      throw new Error("path outside the session boundary");
    });
    const { open } = renderPanel({ saveFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");
    pressSave();

    const failed = await screen.findByTestId("editor-save-failed");
    expect(failed).toHaveTextContent("Could not save");
    // 拒绝的理由是守护给出的那一句，不是一句笼统的「保存失败」
    expect(failed).toHaveTextContent("path outside the session boundary");
    // 没写成就不能说存好了
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");
  });

  it("没改过的标签直接关，不问", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    fireEvent.click(screen.getByTestId("editor-tab-close"));
    expect(screen.queryByTestId("editor-close-ask")).toBeNull();
    expect(screen.queryAllByTestId("editor-tab")).toHaveLength(0);
  });

  it("关有未保存改动的标签先问，选取消之后标签和文字原样都在", async () => {
    const { open, saveFile } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");

    fireEvent.click(screen.getByTestId("editor-tab-close"));
    const ask = await screen.findByTestId("editor-close-ask");
    expect(ask).toHaveTextContent("a.txt");
    // 问的时候什么都没做：没写盘，也没关
    expect(saveFile).not.toHaveBeenCalled();
    expect(screen.getByTestId("editor-tab")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("editor-close-cancel"));
    expect(screen.queryByTestId("editor-close-ask")).toBeNull();
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");
    expect(surface()).toHaveTextContent("gammaX");
  });

  it("关闭时选「不保存」：标签关掉，一个字也不写盘", async () => {
    const { open, saveFile } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");
    fireEvent.click(screen.getByTestId("editor-tab-close"));
    await screen.findByTestId("editor-close-ask");

    fireEvent.click(screen.getByTestId("editor-close-discard"));
    await waitFor(() => expect(screen.queryAllByTestId("editor-tab")).toHaveLength(0));
    expect(saveFile).not.toHaveBeenCalled();
  });

  it("关闭时选「保存」：先写盘，写成了才关", async () => {
    const { open, saveFile } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");
    fireEvent.click(screen.getByTestId("editor-tab-close"));
    await screen.findByTestId("editor-close-ask");

    fireEvent.click(screen.getByTestId("editor-close-save"));
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryAllByTestId("editor-tab")).toHaveLength(0));
  });

  it("关闭时选「保存」但保存失败：标签留下，因为关掉就只剩一份了", async () => {
    const saveFile = vi.fn(async () => {
      throw new Error("disk is full");
    });
    const { open } = renderPanel({ saveFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");
    fireEvent.click(screen.getByTestId("editor-tab-close"));
    await screen.findByTestId("editor-close-ask");

    fireEvent.click(screen.getByTestId("editor-close-save"));
    await screen.findByTestId("editor-save-failed");
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");
  });

  it("磁盘上改过而我没动过：静默重载，不拿问题打扰", async () => {
    let mtime = 100;
    const statFile = vi.fn(async () => ({ exists: true, mtime, size: 10 }));
    const { open, loadFile } = renderPanel({ statFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));

    loadFile.mockResolvedValue({
      content: "changed on disk",
      binary: false,
      truncated: false,
      totalLines: null,
    });
    mtime = 200;
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });

    await waitFor(() => expect(surface()).toHaveTextContent("changed on disk"));
    expect(screen.queryByTestId("editor-disk-conflict")).toBeNull();
    // 静默重载不是改动：重载之后文件不该显示成脏的
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "false");
  });

  it("磁盘上改过而我也改过：问一句，「保留我的」保住我的文字", async () => {
    let mtime = 100;
    const statFile = vi.fn(async () => ({ exists: true, mtime, size: 10 }));
    const { open, loadFile } = renderPanel({ statFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");

    loadFile.mockResolvedValue({
      content: "theirs",
      binary: false,
      truncated: false,
      totalLines: null,
    });
    mtime = 200;
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });

    const bar = await screen.findByTestId("editor-disk-conflict");
    expect(bar).toHaveTextContent("changed on disk");
    expect(screen.getByTestId("editor-conflict-reload")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("editor-conflict-keep"));
    expect(screen.queryByTestId("editor-disk-conflict")).toBeNull();
    expect(surface()).toHaveTextContent("gammaX");
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");

    // 保留过的这一次不再重复问：再探一次不该又弹出同一句
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    expect(screen.queryByTestId("editor-disk-conflict")).toBeNull();
  });

  it("磁盘上改过而我也改过：选「重载」就换成磁盘上的那一份", async () => {
    let mtime = 100;
    const statFile = vi.fn(async () => ({ exists: true, mtime, size: 10 }));
    const { open, loadFile } = renderPanel({ statFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("X");

    loadFile.mockResolvedValue({
      content: "theirs",
      binary: false,
      truncated: false,
      totalLines: null,
    });
    mtime = 200;
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
    });
    await screen.findByTestId("editor-disk-conflict");

    fireEvent.click(screen.getByTestId("editor-conflict-reload"));
    await waitFor(() => expect(surface()).toHaveTextContent("theirs"));
    expect(screen.queryByTestId("editor-disk-conflict")).toBeNull();
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "false");
  });

  it("折行与缩略图两个开关都点得动，换标签再回来还在", async () => {
    const { open } = renderPanel({ sid: "s1" });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    // 默认：折行开、缩略图关
    expect(screen.getByTestId("editor-toggle-wrap")).toHaveAttribute("aria-pressed", "true");
    expect(surface()).toHaveAttribute("data-wrap", "true");
    expect(screen.queryByTestId("editor-minimap")).toBeNull();

    fireEvent.click(screen.getByTestId("editor-toggle-wrap"));
    expect(screen.getByTestId("editor-toggle-wrap")).toHaveAttribute("aria-pressed", "false");
    expect(surface()).toHaveAttribute("data-wrap", "false");
    fireEvent.click(screen.getByTestId("editor-toggle-minimap"));
    expect(screen.getByTestId("editor-toggle-minimap")).toHaveAttribute("aria-pressed", "true");

    // 同一会话里换个标签再回来，两个开关都还记着
    open("/p/emrg/b.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("one"));
    expect(surface()).toHaveAttribute("data-wrap", "false");
    expect(screen.getByTestId("editor-minimap")).toBeInTheDocument();
  });

  it("缩略图把整份文件收成一列条，不按文件长度变长", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    fireEvent.click(screen.getByTestId("editor-toggle-minimap"));
    const map = screen.getByTestId("editor-minimap");
    // 三行的文件，三条；每一条都不是按钮
    expect(map.querySelectorAll(".editor-minimap-bar")).toHaveLength(3);
    expect(within(map).queryByRole("button")).toBeNull();
  });

  it("状态角标写着语言与光标行列，光标一动就跟着变", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.py", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    expect(screen.getByTestId("editor-status-language")).toHaveTextContent("Python");
    expect(screen.getByTestId("editor-status-cursor")).toHaveTextContent("Ln 1, Col 1");

    // 偏移 8 = 第二行第三列（alpha\n 之后是 beta 的第二个字符之后）
    act(() => {
      editor().dispatch({ selection: { anchor: 8 } });
    });
    expect(screen.getByTestId("editor-status-cursor")).toHaveTextContent("Ln 2, Col 3");
  });

  it("认不出的扩展名按纯文本显示，而且照样能改", async () => {
    const { open } = renderPanel();
    open("/p/emrg/notes.zzz", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("plain words"));
    expect(screen.getByTestId("editor-status-language")).toHaveTextContent("Plain text");
    // 认不出不等于不能编辑：降级成只读是清单里点名要避免的那一种
    expect(surface()).toHaveAttribute("data-readonly", "false");
    expect(screen.queryByTestId("editor-readonly-note")).toBeNull();
    typeInto("!");
    expect(screen.getByTestId("editor-tab")).toHaveAttribute("data-dirty", "true");
  });

  it("超过阈值的大文件以只读打开，并在界面上说明是只读", async () => {
    const statFile = vi.fn(async () => ({ exists: true, mtime: 100, size: 5 * 1024 * 1024 }));
    const { open } = renderPanel({ statFile });
    open("/p/emrg/a.txt", "/p/emrg", false);

    const note = await screen.findByTestId("editor-readonly-note");
    expect(note).toHaveTextContent("read only");
    expect(surface()).toHaveAttribute("data-readonly", "true");
    expect(editor().state.readOnly).toBe(true);
  });

  it("只读到开头的文件也是只读：把它整份存回去会删掉后面全部", async () => {
    const { open } = renderPanel({
      shape: { "/p/emrg/a.txt": { truncated: true } },
    });
    open("/p/emrg/a.txt", "/p/emrg", false);

    const note = await screen.findByTestId("editor-readonly-note");
    expect(note).toHaveTextContent("beginning");
    expect(surface()).toHaveAttribute("data-readonly", "true");
  });

  it("二进制文件以只读打开，不把字节当文字显示", async () => {
    const { open } = renderPanel({ shape: { "/p/emrg/a.txt": { binary: true } } });
    open("/p/emrg/a.txt", "/p/emrg", false);

    const note = await screen.findByTestId("editor-readonly-note");
    expect(note).toHaveTextContent("Binary");
    expect(surface()).toHaveAttribute("data-readonly", "true");
  });

  it("多光标与列选是开着的：它们不是一层可以悄悄关掉的东西", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    // 列选与 Option+点击都落在这个开关上：关掉它，两个手势各自静默失效
    expect(editor().state.facet(EditorState.allowMultipleSelections)).toBe(true);
    // 多个选区在同一个文档里同时存在
    act(() => {
      editor().dispatch({
        selection: EditorSelection.create(
          [EditorSelection.range(0, 5), EditorSelection.range(6, 10)],
          0,
        ),
      });
    });
    expect(editor().state.selection.ranges).toHaveLength(2);
  });
});
