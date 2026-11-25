import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { ICON_PX, INDENT_PX, ROW_HEIGHT } from "../lib/fileTree";
// 样式表走 Vite 的 `?raw`，不走 `node:fs`（`tests/test_renderer_node_types.py`）。
import cockpitCss from "../cockpit.css?raw";
import { SessionColumn } from "./SessionColumn";
import { LeftTrees } from "./LeftTrees";
import { StatusBand } from "./StatusBand";
import {
  BODY_MIN,
  LEFT_COLUMN_DEFAULT,
  LEFT_COLUMN_MIN,
  RAIL_WIDTH,
  RIGHT_COLUMN_DEFAULT,
  RIGHT_COLUMN_MIN,
} from "../lib/cockpit";
import type { OpenSessionEntry } from "../lib/sidebar";

/**
 * page.test.tsx — the page's five sections, held from both ends (issue #2969).
 *
 * One rule per assertion, and each is a rule the host named: the sections and their
 * order, the widths written down, two drag handles, a body with no ceiling, a session
 * column that lists open sessions and groups nothing, and an inspector of exactly three
 * tabs. A layout requirement that no test names is a requirement that decays back into
 * whatever the next edit finds convenient, so the numbers here are the contract rather
 * than a description of today's template.
 */

const open: OpenSessionEntry[] = [
  { sid: "s1", title: "Alpha", projectName: "emrg", lastActive: "2026-11-18T07:00:00Z" },
];

describe("the page's geometry", () => {
  it("五段宽度写死在契约里：轨 60 / 会话栏 218 / 检查栏 262 / 两条把手 6", () => {
    expect(RAIL_WIDTH).toBe(60);
    expect(LEFT_COLUMN_DEFAULT).toBe(218);
    expect(RIGHT_COLUMN_DEFAULT).toBe(262);
    // 两栏的最小宽度是唯一兜底，正文不预留任何宽度
    expect(LEFT_COLUMN_MIN).toBe(180);
    expect(RIGHT_COLUMN_MIN).toBe(220);
    expect(BODY_MIN).toBe(0);
  });
});

describe("SessionColumn", () => {
  it("表头只有一行：打开中的条数 + 两个入口，没有分组标签", () => {
    render(
      <I18nProvider lang="en">
        <SessionColumn openSessions={open} onSearch={() => {}} onNew={() => {}} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("session-col-count")).toHaveTextContent("Open 1");
    expect(screen.getByTestId("session-col-search")).toBeInTheDocument();
    expect(screen.getByTestId("session-col-new")).toBeInTheDocument();
    // 旧的两级分组：一个 section 标签加一节列表。栏里只有一节，所以没有标签
    const column = screen.getByTestId("session-column");
    expect(column.querySelectorAll(".conv-group-label, [data-testid='open-sessions-label']")).toHaveLength(0);
    expect(column.textContent).not.toContain("Open sessions");
  });

  it("每个打开中的会话一行，行上不做第二级", () => {
    render(
      <I18nProvider lang="en">
        <SessionColumn openSessions={open} />
      </I18nProvider>,
    );
    const rows = screen.getAllByTestId("open-session-item");
    expect(rows).toHaveLength(1);
    expect(rows[0].querySelectorAll(".session-label")).toHaveLength(1);
  });
});

describe("LeftTrees — 一棵树、三个分组（rant 2026-11-20T17:56:07）", () => {
  const renderTrees = (props: Partial<Parameters<typeof LeftTrees>[0]> = {}) =>
    render(
      <I18nProvider lang="en">
        <LeftTrees agentRoot="/p/emrg" sessionRoot="/p/emrg/.emrg/sessions/s1" {...props} />
      </I18nProvider>,
    );

  it("一棵树、三个分组，顺序固定，段名「目录」", () => {
    renderTrees();
    expect(screen.getByTestId("left-trees-header")).toHaveTextContent("Directories");
    // 一棵树只有一个滚动容器：以前是两段、两棵树、两个滚动条
    const scroller = screen.getByTestId("left-trees-scroll");
    expect(screen.getByTestId("left-trees").querySelectorAll(".left-trees-scroll")).toHaveLength(1);
    const groups = scroller.querySelectorAll(".left-group");
    expect(groups).toHaveLength(3);
    expect(groups[0]).toBe(screen.getByTestId("left-group-agent"));
    expect(groups[1]).toBe(screen.getByTestId("left-group-session"));
    expect(groups[2]).toBe(screen.getByTestId("left-group-extra"));
    expect(screen.getByTestId("left-group-agent-row")).toHaveTextContent("Agent directory");
    expect(screen.getByTestId("left-group-session-row")).toHaveTextContent("Session directory");
    expect(screen.getByTestId("left-group-extra-row")).toHaveTextContent("Extra roots");
  });

  it("默认收放：agent 目录收起，会话目录与额外根目录展开", () => {
    renderTrees({ sessionRoots: ["/p/other"] });
    expect(screen.getByTestId("left-group-agent")).toHaveAttribute("data-collapsed", "true");
    // 收起的那一组里什么都不画——连树都没有，不是藏起来
    expect(screen.queryByTestId("left-tree-agent")).toBeNull();
    expect(screen.queryByTestId("left-group-agent-body")).toBeNull();
    expect(screen.getByTestId("left-group-session")).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByTestId("left-tree-session")).toBeInTheDocument();
    expect(screen.getByTestId("left-group-extra")).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByTestId("left-tree-extra")).toBeInTheDocument();
  });

  it("收起的分组只占一行：表头留在原处，树整个卸掉，三组都还在", () => {
    renderTrees();
    const header = screen.getByTestId("left-group-session-row");
    const parent = header.parentElement;
    const indexBefore = Array.from(parent?.children ?? []).indexOf(header);
    expect(screen.getByTestId("left-tree-session")).toBeInTheDocument();
    fireEvent.click(header);
    expect(screen.queryByTestId("left-tree-session")).toBeNull();
    expect(screen.queryByTestId("left-group-session-body")).toBeNull();
    // 表头的父节点与序号都没变：它的纵向位置就是那一行
    expect(header.parentElement).toBe(parent);
    expect(Array.from(parent?.children ?? []).indexOf(header)).toBe(indexBefore);
    expect(screen.getByTestId("left-trees-scroll").querySelectorAll(".left-group")).toHaveLength(3);
    // 旧的按高度分配不留残迹：分组不写内联 flex（`sectionFlex` 已删）
    expect(screen.getByTestId("left-group-session").getAttribute("style")).toBeNull();
    expect(screen.getByTestId("left-group-agent").getAttribute("style")).toBeNull();
  });

  it("点分组节点只收放：不给右栏换文件，也不选会话", () => {
    const onOpenFile = vi.fn();
    renderTrees({ onOpenFile });
    fireEvent.click(screen.getByTestId("left-group-agent-row"));
    expect(screen.getByTestId("left-group-agent")).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByTestId("left-tree-agent")).toBeInTheDocument();
    // 分组节点不是文件行：右栏当前打开的文件一个都没变
    expect(onOpenFile).not.toHaveBeenCalled();
  });

  it("额外根目录只列当前会话自己加的根（不是安装级默认目录）", () => {
    renderTrees({ sessionRoots: ["/p/other"] });
    const row = screen.getByTestId("left-tree-extra-root");
    expect(row).toHaveTextContent("other");
    // 悬停给出全路径：行上写的是目录名，全路径只在提示里
    expect(row).toHaveAttribute("title", "/p/other");
  });

  it("没有额外根时：一行「没有」，不是一棵可以点开的空树", () => {
    renderTrees({ sessionRoots: [] });
    const empty = screen.getByTestId("left-group-extra-empty");
    expect(empty).toHaveTextContent("None");
    expect(screen.queryByTestId("left-tree-extra")).toBeNull();
    expect(empty.tagName).toBe("DIV");
    expect(empty.querySelectorAll("button, [tabindex], a")).toHaveLength(0);
  });

  it("会话目录那组只放会话目录自己：额外根不再挂在它下面", () => {
    renderTrees({ sessionRoots: ["/p/other", "/p/third"] });
    const root = screen.getByTestId("left-tree-session-root");
    expect(root).toHaveTextContent("s1");
    expect(root).toHaveAttribute("title", "/p/emrg/.emrg/sessions/s1");
    // 以前这里会画成一棵三个根的树（issue #3063 的口径）
    expect(screen.queryByTestId("left-tree-session-roots")).toBeNull();
    expect(screen.getByTestId("left-tree-extra-roots")).toHaveAttribute("data-root-count", "2");
  });

  it("没有会话时不画空树，而是说出来", () => {
    render(
      <I18nProvider lang="en">
        <LeftTrees />
      </I18nProvider>,
    );
    // agent 目录默认收起，展开之后才看得到那句话
    fireEvent.click(screen.getByTestId("left-group-agent-row"));
    expect(screen.getByTestId("left-tree-agent-no-session")).toHaveTextContent("Select a session");
  });
});

/**
 * 分组节点就是树里的第一行（rant 2026-11-20T21:27:29）：清单逐条对应一个 it。
 *
 * 之前它是树体上面的一行表头：更高的行、没有缩进、没有图标，名字底下还挂着一个工具条
 * （刷新 + 全部收起），而真实目录的名字反倒没有行来写。现在分组值一行的高度，下一层就是
 * 真实目录那一行。每一条要求都有自己的一条断言：一条断言守两条要求，改坏其中一条时
 * 另一条会替它挡住。
 */
describe("LeftTrees — 分组节点是树里的第一行（rant 2026-11-20T21:27:29）", () => {
  const SESSION = "/p/emrg/.emrg/sessions/s1";

  const loadDir = async (p: string) => ({
    entries: p === SESSION ? [{ name: "notes.md", path: `${SESSION}/notes.md`, type: "file" as const }] : [],
    truncated: false,
  });

  const renderTrees = (props: Partial<Parameters<typeof LeftTrees>[0]> = {}) =>
    render(
      <I18nProvider lang="en">
        <LeftTrees agentRoot="/p/emrg" sessionRoot={SESSION} loadDir={loadDir} {...props} />
      </I18nProvider>,
    );

  /** 一条规则的声明块；选择器不在文件里就是红，不是「没找到就跳过」。 */
  const rule = (selector: string): string => {
    const start = cockpitCss.indexOf(`\n${selector} {`);
    expect(start, `${selector} 不在 cockpit.css 里`).toBeGreaterThan(-1);
    return cockpitCss.slice(start, cockpitCss.indexOf("}", start));
  };

  const pad = (el: HTMLElement): number => parseFloat(el.style.paddingLeft);

  it("清单 2：左边三角、16px 文件夹图标、再是名字，顺序固定", async () => {
    renderTrees();
    const row = screen.getByTestId("left-group-session-row");
    // 行内三件东西的顺序就是要求本身：三角最左，图标其次，名字最后
    const parts = Array.from(row.children).map((c) => (c as HTMLElement).className);
    expect(parts).toEqual(["ft-caret", "ft-glyph", "ft-name"]);
    expect(row.querySelector(".ft-caret")?.textContent).toBe("▾");
    const glyph = row.querySelector(".ft-glyph") as HTMLElement;
    expect(glyph.style.width).toBe(`${ICON_PX}px`);
    expect(glyph.style.height).toBe(`${ICON_PX}px`);
    expect(glyph).toHaveAttribute("data-kind", "dir-open");
    expect(row.querySelector(".ft-name")?.textContent).toBe("Session directory");
    await waitFor(() => expect(screen.getAllByTestId("left-tree-session-row")).toHaveLength(1));
  });

  it("清单 2：每行 22px，按层缩进一个步长，三层都量到", async () => {
    renderTrees();
    await waitFor(() => expect(screen.getAllByTestId("left-tree-session-row")).toHaveLength(1));
    const group = screen.getByTestId("left-group-session-row");
    const root = screen.getByTestId("left-tree-session-root");
    const file = screen.getAllByTestId("left-tree-session-row")[0];
    for (const el of [group, root, file]) expect(el.style.height).toBe(`${ROW_HEIGHT}px`);
    expect(group).toHaveAttribute("data-depth", "0");
    expect(root).toHaveAttribute("data-depth", "1");
    expect(file).toHaveAttribute("data-depth", "2");
    // 分组行就在面板左边缘，目录行比它多一个步长，文件行再多一个
    expect(pad(group)).toBeLessThan(pad(root));
    expect(pad(root) - pad(group)).toBe(INDENT_PX);
    expect(pad(file) - pad(root)).toBe(INDENT_PX);
  });

  it("清单 3：与普通行同一套结构，只多一个 left-group-row 类；整行浅底共用 ft-row", async () => {
    renderTrees();
    await waitFor(() => expect(screen.getAllByTestId("left-tree-session-row")).toHaveLength(1));
    const group = screen.getByTestId("left-group-session-row");
    const ordinary = screen.getAllByTestId("left-tree-session-row")[0];
    expect(group.tagName).toBe(ordinary.tagName);
    expect(group).toHaveAttribute("role", "treeitem");
    expect(ordinary).toHaveAttribute("role", "treeitem");
    // 悬停整行浅底来自 `.ft-row`：分组行与普通行都有它，谁也不多一层底色
    expect(group).toHaveClass("ft-row");
    expect(ordinary).toHaveClass("ft-row");
    // 只有分组行多这一个类，字体全在它身上
    expect(group).toHaveClass("left-group-row");
    expect(ordinary).not.toHaveClass("left-group-row");
    const type = rule(".left-group-row");
    expect(type).toContain("font-size: 11.5px");
    expect(type).toContain("font-weight: 700");
    expect(type).toContain("color: var(--text-2)");
    // 几何不在这条规则里重写：行高、缩进、图标仍只有 TreeLine 内联样式那一处
    expect(type).not.toContain("height:");
    expect(type).not.toContain("padding");
    expect(type).not.toContain("width:");
  });

  it("清单 4：分组的下一层就是真实目录，行上写目录名，内容挂在它下面", async () => {
    renderTrees();
    await waitFor(() => expect(screen.getAllByTestId("left-tree-session-row")).toHaveLength(1));
    const body = screen.getByTestId("left-group-session-body");
    const root = screen.getByTestId("left-tree-session-root");
    expect(body).toContainElement(root);
    // 行上写的是目录名，全路径只在提示里
    expect(root.textContent).toContain("s1");
    expect(root).toHaveAttribute("title", SESSION);
    expect(body).toContainElement(screen.getAllByTestId("left-tree-session-row")[0]);
  });

  it("清单 5：分组工具条删除——每组一条的刷新/全部收起不再有，面板头部只有一个刷新（#3108 / #3226）", () => {
    renderTrees({ sessionRoots: ["/p/other"] });
    const panel = screen.getByTestId("left-trees");
    // #3108 否掉的是每组一条工具条，连同装按钮的 .ft-tool 一起不许回来
    expect(panel.querySelectorAll(".ft-toolbar, .ft-tool, .left-group-header")).toHaveLength(0);
    // 这条主张在 #3226 里改了口径：当时是「一个按钮都没有」，因为刷新随工具条一起被删；
    // 现在整块面板头部有一个刷新，它不属于任何一组，也不在树里
    const buttons = panel.querySelectorAll("button");
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toBe(screen.getByTestId("left-trees-refresh"));
    expect(screen.getByTestId("left-trees-header")).toContainElement(buttons[0]);
  });

  it("清单 5b：头部的刷新点一下，画出来的那棵树重读自己（issue #3226）", async () => {
    const load = vi.fn(async () => ({ entries: [], truncated: false }));
    renderTrees({ loadDir: load });
    await waitFor(() => expect(load).toHaveBeenCalled());
    const before = load.mock.calls.length;
    fireEvent.click(screen.getByTestId("left-trees-refresh"));
    // 只重读画出来的东西：agent 那一组默认收起，没有树，就没有它的读盘
    await waitFor(() => expect(load.mock.calls.length).toBe(before + 1));
    expect(load).toHaveBeenLastCalledWith("/p/emrg/.emrg/sessions/s1");
  });

  it("清单 6：「没有」也是一行 22px：斜体、次要字色、按不动", () => {
    renderTrees({ sessionRoots: [] });
    const empty = screen.getByTestId("left-group-extra-empty");
    expect(empty.style.height).toBe(`${ROW_HEIGHT}px`);
    expect(empty).toHaveClass("left-group-empty");
    expect(empty).toHaveTextContent("None");
    // 它是一行说明，不是一行可点的行：没有悬停浅底、没有三角可点、点不动。
    // 三角的元素还在（每一行都由同一个 TreeLine 画），但它里面是空的——没有东西可折。
    expect(empty).not.toHaveClass("ft-row");
    expect(empty.querySelector(".ft-caret")?.textContent).toBe("");
    expect(empty.querySelectorAll("button, [tabindex]")).toHaveLength(0);
    const style = rule(".left-group-empty");
    expect(style).toContain("font-style: italic");
    expect(style).toContain("color: var(--text-3)");
    // 行高来自 TreeLine，不在这条规则里另写一个数
    expect(style).not.toContain("height:");
  });
});

describe("StatusBand", () => {
  it("右端只有两个栏开关，别无他物（issue #3028）", () => {
    const noop = () => {};
    render(
      <I18nProvider lang="en">
        <StatusBand
          running={false}
          usage={null}
          leftOpen
          rightOpen
          onToggleLeft={noop}
          onToggleRight={noop}
          onOpenRanking={() => {}}
        />
      </I18nProvider>,
    );
    const band = screen.getByTestId("status-band");
    // 右端那一块：恰好两个开关。三段读数的按钮在左边——它们是读数，不是这一端的动作
    // （rant 2026-11-20T17:30:50），所以这条把范围收到 `.band-right` 上，仍然一个不多。
    expect(band.querySelector(".band-right")?.querySelectorAll("button")).toHaveLength(2);
    expect(band.querySelectorAll("button")).toHaveLength(5);
  });
});
