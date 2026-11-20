import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
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
    expect(screen.getByTestId("left-group-agent-header")).toHaveTextContent("Agent directory");
    expect(screen.getByTestId("left-group-session-header")).toHaveTextContent("Session directory");
    expect(screen.getByTestId("left-group-extra-header")).toHaveTextContent("Extra roots");
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
    const header = screen.getByTestId("left-group-session-header");
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
    fireEvent.click(screen.getByTestId("left-group-agent-header"));
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
    fireEvent.click(screen.getByTestId("left-group-agent-header"));
    expect(screen.getByTestId("left-tree-agent-no-session")).toHaveTextContent("Select a session");
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
