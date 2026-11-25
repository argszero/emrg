import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { EditorSelection, EditorState } from "@codemirror/state";
import { I18nProvider } from "../lib/i18n";
import {
  EditorPanel,
  type EditorPanelHandle,
  type FileContent,
  type ReadWindow,
} from "./EditorPanel";
import { LeftTrees } from "./LeftTrees";
import { DEFAULT_FONT_SIZE } from "../lib/editorDoc";

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
  // 四空格缩进的文件：状态角标读的是这份文件自己的缩进，不是常量
  "/p/emrg/wide.py": "def f():\n    return 1\n",
  // CRLF 的文件：行尾那一格跟着它变
  "/p/emrg/win.txt": "one\r\ntwo\r\n",
};

interface PanelOptions {
  sid?: string | null;
  /** What the reader answers about one path: a binary frame, or a window instead of a file. */
  shape?: Record<
    string,
    { binary?: boolean; truncated?: boolean; totalLines?: number | null; size?: number | null }
  >;
  /** Paths the reader refuses, and the daemon's own code for the refusal. */
  fail?: Record<string, string>;
  /** Answer the reader itself, when one file has to answer differently per page. */
  loadFile?: (path: string, window?: ReadWindow) => Promise<FileContent>;
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
  const loadFile = vi.fn(async (p: string, window?: ReadWindow) => {
    const code = options.fail?.[p];
    if (code) {
      // A failed read reaches the panel as a thrown error carrying the daemon's code: the
      // bridge re-throws what the IPC channel hands back as data (issue #3210), because a
      // property attached to an Error does not survive that boundary.
      const failure = new Error("the daemon refused this read") as Error & {
        errorCode?: string | null;
      };
      failure.errorCode = code;
      throw failure;
    }
    if (options.loadFile) return options.loadFile(p, window);
    const shape = options.shape?.[p];
    return {
      content: FILES[p] ?? "",
      binary: shape?.binary ?? false,
      truncated: shape?.truncated ?? false,
      totalLines: shape?.totalLines ?? null,
      size: shape?.size ?? null,
    };
  });
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
      size: null,
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
      size: null,
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
      size: null,
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
    // 默认：不折行、缩略图关
    expect(screen.getByTestId("editor-toggle-wrap")).toHaveAttribute("aria-pressed", "false");
    expect(surface()).toHaveAttribute("data-wrap", "false");
    expect(screen.queryByTestId("editor-minimap")).toBeNull();

    fireEvent.click(screen.getByTestId("editor-toggle-wrap"));
    expect(screen.getByTestId("editor-toggle-wrap")).toHaveAttribute("aria-pressed", "true");
    expect(surface()).toHaveAttribute("data-wrap", "true");
    fireEvent.click(screen.getByTestId("editor-toggle-minimap"));
    expect(screen.getByTestId("editor-toggle-minimap")).toHaveAttribute("aria-pressed", "true");

    // 同一会话里换个标签再回来，两个开关都还记着
    open("/p/emrg/b.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("one"));
    expect(surface()).toHaveAttribute("data-wrap", "true");
    expect(screen.getByTestId("editor-minimap")).toBeInTheDocument();
  });

  it("缩略图画的是文件自己的文字：一行一条，不是每行长度的条形", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    fireEvent.click(screen.getByTestId("editor-toggle-minimap"));
    const map = screen.getByTestId("editor-minimap");
    // 三行的文件，三条；每一条都是那一行自己的字，读得出来（issue #3200 第 3 条）
    const lines = map.querySelectorAll(".editor-minimap-line");
    expect(lines).toHaveLength(3);
    expect([...lines].map((el) => el.textContent)).toEqual(["alpha", "beta", "gamma"]);
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

  it("超过页预算的大文件照样打得开：多大、共几行、显示开头几行，都写在界面上", async () => {
    const big = 5 * 1024 * 1024;
    const { open } = renderPanel({
      shape: { "/p/emrg/a.txt": { truncated: true, totalLines: 120000, size: big } },
      statFile: vi.fn(async () => ({ exists: true, mtime: 100, size: big })),
    });
    open("/p/emrg/a.txt", "/p/emrg", false);

    // 打得开是第一步：拒绝读不再是一个选项（issue #3210）
    const note = await screen.findByTestId("editor-readonly-note");
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    expect(note).toHaveTextContent("5.0 MB");
    expect(note).toHaveTextContent("120000 lines");
    expect(note).toHaveTextContent("lines 1–3");
    // 只读的理由还是那个：把这一页存回去会删掉后面 119997 行
    expect(surface()).toHaveAttribute("data-readonly", "true");
  });

  it("没到编辑器阈值的长文件，说明里只有行数，不谈大小", async () => {
    const { open } = renderPanel({
      shape: { "/p/emrg/a.txt": { truncated: true, totalLines: 9000, size: 200 * 1024 } },
    });
    open("/p/emrg/a.txt", "/p/emrg", false);

    const note = await screen.findByTestId("editor-readonly-note");
    expect(note).toHaveTextContent("Lines 1–3 of 9000");
    expect(note).not.toHaveTextContent("KB");
    expect(note).not.toHaveTextContent("read-only");
  });

  it("读不出来时说得出是哪一种失败，不是全都一句「读不出这个文件」", async () => {
    const { open } = renderPanel({
      fail: { "/p/emrg/gone.txt": "not_found", "/p/emrg/dir": "is_directory" },
    });
    open("/p/emrg/gone.txt", "/p/emrg", false);
    expect(await screen.findByTestId("editor-failed")).toHaveTextContent(
      "That file does not exist",
    );

    open("/p/emrg/dir", "/p/emrg", false);
    const second = await screen.findByTestId("editor-failed");
    expect(second).toHaveTextContent("That is a directory, not a file");
    expect(second).not.toHaveTextContent("That file does not exist");
  });

  it("分页往下走：按钮把下一页要回来，页码跟着走，中途那页照样只读", async () => {
    const big = 5 * 1024 * 1024;
    const pages: Record<number, string> = {
      1: "line 1\nline 2\nline 3",
      4: "line 4\nline 5\nline 6",
    };
    const loadFile = vi.fn(async (_p: string, window?: ReadWindow) => ({
      content: pages[window?.startLine ?? 1] ?? "",
      binary: false,
      truncated: true,
      totalLines: 10,
      size: big,
    }));
    const { open } = renderPanel({
      loadFile,
      statFile: vi.fn(async () => ({ exists: true, mtime: 100, size: big })),
    });
    open("/p/emrg/a.txt", "/p/emrg", false);

    expect(await screen.findByTestId("editor-readonly-note")).toHaveTextContent("lines 1–3");
    fireEvent.click(screen.getByTestId("editor-next-page"));

    // 第二次请求带的是上一页的下一页，不是从第一行重来一遍
    await waitFor(() =>
      expect(loadFile).toHaveBeenLastCalledWith("/p/emrg/a.txt", {
        startLine: 4,
        lineLimit: 2000,
      }),
    );
    await waitFor(() => expect(surface()).toHaveTextContent("line 4"));
    expect(screen.getByTestId("editor-readonly-note")).toHaveTextContent("lines 4–6");
    // 中途那一页仍然只读：把这一页存回去会删掉文件其余部分
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

/**
 * The ten gaps issue #3200 names (2026-11-24).
 *
 * One test per item, and each drives the surface the host drives: a click on the button the
 * item says is missing, a right-click on a tab, a keystroke through CodeMirror's own keymap.
 * Nothing here reads the source it is testing — a grep would pass for a panel whose new
 * button is wired to nothing, which is the shape the ten items are about.
 *
 * Items 1, 4 and 5 have no test in this file: the first is a stylesheet rule (see
 * `cockpit.test.ts`), and the other two are CodeMirror wiring (see `CodeEditor.test.tsx`).
 */
describe("EditorPanel's ten gaps (issue #3200)", () => {
  it("2. 保存与重载都在工具栏上，重载遇到未保存改动先问", async () => {
    const saveFile = vi.fn(
      async (_path: string, _content: string, _sid: string) => ({ mtime: 200, size: 20 }),
    );
    const { open, loadFile } = renderPanel({ saveFile });
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));

    // 有改动时点重载：先问，答「取消」就把改动留着
    typeInto("?");
    expect(screen.getByTestId("editor-tab-dirty")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("editor-action-reload"));
    expect(screen.getByTestId("editor-reload-ask")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("editor-reload-cancel"));
    expect(screen.queryByTestId("editor-reload-ask")).toBeNull();
    expect(surface()).toHaveTextContent("gamma?");

    // 保存按钮与 Cmd+S 是同一条路径：写出去的是缓冲区里的那一份
    fireEvent.click(screen.getByTestId("editor-action-save"));
    await waitFor(() => expect(saveFile).toHaveBeenCalledTimes(1));
    expect(saveFile.mock.calls[0][0]).toBe("/p/emrg/a.txt");
    expect(String(saveFile.mock.calls[0][1])).toContain("gamma?");
    // 写入回来之前标签还是脏的：等那个点消失，才是「已经没有可丢的东西」
    await waitFor(() => expect(screen.queryByTestId("editor-tab-dirty")).toBeNull());

    // 干净了再点重载：没有可丢的东西，就不问
    const reads = loadFile.mock.calls.length;
    fireEvent.click(screen.getByTestId("editor-action-reload"));
    expect(screen.queryByTestId("editor-reload-ask")).toBeNull();
    await waitFor(() => expect(loadFile.mock.calls.length).toBe(reads + 1));
  });

  it("3. 缩略图画的是文件的文字，点它仍然跳到那一行", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    fireEvent.click(screen.getByTestId("editor-toggle-minimap"));
    const code = screen.getByTestId("editor-minimap-code");
    // 图上读得出文件自己的字：这一条就是「不是长度直方图」的判据
    expect(code.textContent).toContain("alpha");
    expect(code.textContent).toContain("gamma");

    // 点在下半部分：光标往下走，不是停在第一行。jsdom 不算布局，所以框是给出来的
    vi.spyOn(code, "getBoundingClientRect").mockReturnValue({
      top: 0,
      bottom: 90,
      left: 0,
      right: 40,
      width: 40,
      height: 90,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect);
    fireEvent.click(code, { clientY: 85 });
    await waitFor(() => expect(editor().state.selection.main.head).toBeGreaterThan(0));
  });

  it("6. 换标签回来 Cmd+Z 还能走回去，走的只是这份文件自己的历史", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("!");
    expect(surface()).toHaveTextContent("gamma!");

    // 两份文件两个标签，点回去等于换标签（编辑器随之重新挂载）
    open("/p/emrg/b.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("one"));
    fireEvent.click(screen.getAllByTestId("editor-tab")[0]);
    await waitFor(() => expect(surface()).toHaveTextContent("gamma!"));

    fireEvent.keyDown(editor().contentDOM, { key: "z", ctrlKey: true });
    await waitFor(() => expect(surface()).not.toHaveTextContent("gamma!"));
    expect(surface()).toHaveTextContent("alpha");
    // 撤销里没有另一份文件的文字
    expect(surface()).not.toHaveTextContent("two");
  });

  it("7. 标签右键有菜单，能关掉右侧标签；标签能拖着换位", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    open("/p/emrg/b.txt", "/p/emrg", false);
    open("/p/emrg/notes.zzz", "/p/emrg", false);
    await waitFor(() => expect(screen.getAllByTestId("editor-tab")).toHaveLength(3));

    // 右键第二个标签：菜单出来了，选「关闭右侧标签」——第三个没了，前两个还在
    fireEvent.contextMenu(screen.getAllByTestId("editor-tab")[1]);
    expect(screen.getByTestId("editor-tab-menu")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByTestId("editor-tab-menu-close-right"));
    expect(screen.getAllByTestId("editor-tab")).toHaveLength(2);

    // 把第一个标签拖到第二个上：它排到后面去，顺序是面板自己的状态
    const before = screen.getAllByTestId("editor-tab");
    fireEvent.dragStart(before[0]);
    fireEvent.dragOver(before[1]);
    fireEvent.drop(before[1]);
    const after = screen.getAllByTestId("editor-tab").map((el) => el.textContent ?? "");
    expect(after[0]).toContain("b.txt");
    expect(after[1]).toContain("a.txt");
  });

  it("8. 状态角标写着缩进宽度、行尾与编码", async () => {
    const { open } = renderPanel();
    open("/p/emrg/wide.py", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("return 1"));
    // 宽度是这份文件自己的：四空格，不是常量 2
    expect(screen.getByTestId("editor-status-indent")).toHaveTextContent("Spaces: 4");
    expect(screen.getByTestId("editor-status-eol")).toHaveTextContent("LF");
    expect(screen.getByTestId("editor-status-encoding")).toHaveTextContent("UTF-8");

    // 换成 CRLF 的文件：行尾那一格跟着这份文件变
    open("/p/emrg/win.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("two"));
    expect(screen.getByTestId("editor-status-eol")).toHaveTextContent("CRLF");
  });

  it("9. 字号能放大、缩小、归位，换标签也还是那个字号", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    const size = (): number =>
      Number.parseFloat(surface().style.getPropertyValue("--editor-font-size"));
    const press = (key: string): void => {
      fireEvent.keyDown(editor().contentDOM, { key, ctrlKey: true });
    };
    expect(size()).toBe(DEFAULT_FONT_SIZE);

    press("=");
    await waitFor(() => expect(size()).toBe(DEFAULT_FONT_SIZE + 1));
    press("-");
    press("-");
    await waitFor(() => expect(size()).toBe(DEFAULT_FONT_SIZE - 1));
    press("0");
    await waitFor(() => expect(size()).toBe(DEFAULT_FONT_SIZE));

    // 换标签回来还是这个字号：它属于这个面板，不属于某一个标签
    press("=");
    await waitFor(() => expect(size()).toBe(DEFAULT_FONT_SIZE + 1));
    open("/p/emrg/b.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("one"));
    expect(size()).toBe(DEFAULT_FONT_SIZE + 1);
  });

  it("10. 放弃改动是自己的入口：先问，确认后回到磁盘那份；没改动时不问", async () => {
    const { open } = renderPanel();
    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(surface()).toHaveTextContent("alpha"));
    typeInto("!");
    expect(surface()).toHaveTextContent("gamma!");

    fireEvent.click(screen.getByTestId("editor-action-discard"));
    expect(screen.getByTestId("editor-discard-ask")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("editor-discard-confirm"));
    await waitFor(() => expect(surface()).not.toHaveTextContent("gamma!"));
    expect(surface()).toHaveTextContent("alpha");

    // 已经和磁盘一样了：再点不问
    fireEvent.click(screen.getByTestId("editor-action-discard"));
    expect(screen.queryByTestId("editor-discard-ask")).toBeNull();
  });
});

/**
 * Browser mode (issue #3214).
 *
 * The page is a native view that no jsdom can draw, so what is measured here is the panel's
 * half of the contract: which tabs offer the mode, what turning it on tells the main process,
 * what leaving tells it, what the address bar sends, and that the cell the view covers is
 * reported as it was measured.
 */
describe("HTML tab 的浏览器模式（issue #3214）", () => {
  // Every stub states its parameter, even the ones whose answer ignores it. `tsc` reads
  // `mock.calls` against the declared parameter list, so a stub declared with none has the
  // call tuple `[]` — and a test that reads `calls[0][0]` then fails to compile while
  // `vitest run` executes it happily. That is a red only CI sees (measured: run 45540474378).
  function stubBridge(answer: { ok: boolean; error?: string } = { ok: true }) {
    const calls = {
      show: vi.fn(async (_payload: { path: string }) => answer),
      hide: vi.fn(async (_payload?: undefined) => ({ ok: true })),
      navigate: vi.fn(async (_payload: { target: string }) => answer),
      reload: vi.fn(async (_payload: { target?: string }) => answer),
      bounds: vi.fn(
        async (_payload: { x: number; y: number; width: number; height: number }) => ({ ok: true }),
      ),
    };
    (window as unknown as { emrg?: unknown }).emrg = {
      showBrowser: calls.show,
      hideBrowser: calls.hide,
      browserNavigate: calls.navigate,
      browserReload: calls.reload,
      browserBounds: calls.bounds,
    };
    return calls;
  }

  afterEach(() => {
    delete (window as unknown as { emrg?: unknown }).emrg;
  });

  it("只有页面文件带浏览器开关，别的文件没有", async () => {
    stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    // 开关跟着文件走，不是面板的属性：换到另一个 tab 就没有了
    open("/p/emrg/a.txt");
    await waitFor(() => expect(screen.queryByTestId("editor-toggle-browser")).toBeNull());
  });

  it("打开这个模式，把这个 tab 的文件交给主进程，地址栏随之出现", async () => {
    const calls = stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());

    fireEvent.click(screen.getByTestId("editor-toggle-browser"));

    await waitFor(() => expect(calls.show).toHaveBeenCalledWith({ path: "/p/emrg/index.html" }));
    expect(screen.getByTestId("editor-toggle-browser")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("editor-address")).toBeInTheDocument();
    // 那一格是量出来的那块，页面盖在它上面
    expect(screen.getByTestId("editor-browser")).toBeInTheDocument();
  });

  it("切到别的 tab 只隐藏，不销毁；切回来还在浏览器模式", async () => {
    const calls = stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html", "/p/emrg", false);
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("editor-toggle-browser"));
    await waitFor(() => expect(calls.show).toHaveBeenCalled());
    calls.hide.mockClear();

    open("/p/emrg/a.txt", "/p/emrg", false);
    await waitFor(() => expect(calls.hide).toHaveBeenCalled());

    open("/p/emrg/index.html", "/p/emrg", false);
    await waitFor(() => expect(screen.getByTestId("editor-browser")).toBeInTheDocument());
    expect(screen.getByTestId("editor-toggle-browser")).toHaveAttribute("aria-pressed", "true");
  });

  it("地址栏回车把文本原样交给主进程；打不开时把理由写在下面", async () => {
    const calls = stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("editor-toggle-browser"));
    await waitFor(() => expect(screen.getByTestId("editor-address")).toBeInTheDocument());

    const bar = screen.getByTestId("editor-address");
    fireEvent.change(bar, { target: { value: "example.com" } });
    fireEvent.keyDown(bar, { key: "Enter" });
    // 原样交出去：怎么读这段话是策略模块的事，面板不改写它
    await waitFor(() => expect(calls.navigate).toHaveBeenCalledWith({ target: "example.com" }));

    calls.navigate.mockResolvedValueOnce({ ok: false, error: "empty" });
    fireEvent.change(bar, { target: { value: "  " } });
    fireEvent.keyDown(bar, { key: "Enter" });
    await waitFor(() =>
      expect(screen.getByTestId("editor-browser-error")).toHaveTextContent("Type an address first"),
    );
  });

  it("页面盖住的那一格，是渲染器量出来再报上去的", async () => {
    const calls = stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("editor-toggle-browser"));

    await waitFor(() => expect(calls.bounds).toHaveBeenCalled());
    const box = calls.bounds.mock.calls[0][0];
    // 四个数字，来自 getBoundingClientRect()——主进程自己算不出来这一格在哪
    expect(Object.keys(box).sort()).toEqual(["height", "width", "x", "y"]);
    for (const key of ["x", "y", "width", "height"] as const) {
      expect(typeof box[key]).toBe("number");
    }
  });

  it("没有这条通道的构建里，开关照样能点，面板不炸", async () => {
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("editor-toggle-browser"));
    await waitFor(() => expect(screen.getByTestId("editor-browser")).toBeInTheDocument());
    expect(screen.queryByTestId("editor-browser-error")).toBeNull();
  });

  it("「重载」要的是把屏幕上的那一页再读一次，不交地址栏里那段没生效的文本（issue #3229）", async () => {
    const calls = stubBridge();
    const { open } = renderPanel();
    open("/p/emrg/index.html");
    await waitFor(() => expect(screen.getByTestId("editor-toggle-browser")).toBeInTheDocument());
    fireEvent.click(screen.getByTestId("editor-toggle-browser"));
    await waitFor(() => expect(screen.getByTestId("editor-address")).toBeInTheDocument());
    // 地址栏里已经打了一段、但没回车：重载要读的是正在显示的那一页，不是这段还没生效的文本
    fireEvent.change(screen.getByTestId("editor-address"), { target: { value: "example.com" } });

    fireEvent.click(screen.getByTestId("editor-browser-reload"));
    // 不带目标：屏幕上是什么，是主进程知道的事
    await waitFor(() => expect(calls.reload).toHaveBeenCalledWith({}));
    expect(calls.navigate).not.toHaveBeenCalled();

    // 重读失败也要有话说，不能只是原地不动
    calls.reload.mockResolvedValueOnce({ ok: false, error: "invalid" });
    fireEvent.click(screen.getByTestId("editor-browser-reload"));
    await waitFor(() => expect(screen.getByTestId("editor-browser-error")).toBeInTheDocument());
  });
});
