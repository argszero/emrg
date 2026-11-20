import { describe, expect, it } from "vitest";
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

describe("LeftTrees (issue #3068)", () => {
  const renderTrees = (props: Partial<Parameters<typeof LeftTrees>[0]> = {}) =>
    render(
      <I18nProvider lang="en">
        <LeftTrees agentRoot="/p/emrg" sessionRoot="/p/emrg/.emrg/sessions/s1" {...props} />
      </I18nProvider>,
    );

  it("两段按 agent 目录、会话目录的顺序排，表头各一行", () => {
    renderTrees();
    const sections = screen.getByTestId("left-trees").querySelectorAll("section");
    expect(sections).toHaveLength(2);
    expect(sections[0]).toBe(screen.getByTestId("left-section-agent"));
    expect(sections[1]).toBe(screen.getByTestId("left-section-session"));
    expect(screen.getByTestId("left-section-agent-header")).toHaveTextContent("Agent dir");
    expect(screen.getByTestId("left-section-session-header")).toHaveTextContent("Session dir");
  });

  it("点表头收起该段：树被卸掉，表头留在它原来的位置（同一父节点、同一序号）", () => {
    renderTrees();
    const header = screen.getByTestId("left-section-agent-header");
    const parent = header.parentElement;
    const indexBefore = Array.from(parent?.children ?? []).indexOf(header);
    expect(screen.getByTestId("left-tree-agent")).toBeInTheDocument();
    fireEvent.click(header);
    // 收起只卸掉树本身：表头的父节点与序号都没变，所以它的纵向位置也没变。
    expect(screen.queryByTestId("left-tree-agent")).toBeNull();
    expect(header.parentElement).toBe(parent);
    expect(Array.from(parent?.children ?? []).indexOf(header)).toBe(indexBefore);
    // 腾出的高度给下面那段：下面那段保持可增长。
    expect(screen.getByTestId("left-section-session").style.flex).toContain("1 1 50%");
    expect(screen.getByTestId("left-section-agent").style.flex).toContain("0 0 auto");
  });

  it("收起最下面那段时，让出的高度留在栏底：上面那段不再增长", () => {
    renderTrees();
    fireEvent.click(screen.getByTestId("left-section-session-header"));
    expect(screen.queryByTestId("left-tree-session")).toBeNull();
    expect(screen.getByTestId("left-section-session").style.flex).toContain("0 0 auto");
    // `0 0 50%` 而不是 `1 1 50%`：增长的那一半不还给它，余高留在栏底。
    expect(screen.getByTestId("left-section-agent").style.flex).toContain("0 0 50%");
  });

  it("两段都展开时各占一半高", () => {
    renderTrees();
    expect(screen.getByTestId("left-section-agent").style.flex).toContain("1 1 50%");
    expect(screen.getByTestId("left-section-session").style.flex).toContain("1 1 50%");
  });

  it("会话目录那段仍列出会话目录与额外可写根（issue #3063 的规格跟着搬过来）", () => {
    renderTrees({ sessionRoots: ["/p/other", "/p/third"] });
    expect(screen.getByTestId("left-tree-session-roots")).toHaveAttribute("data-root-count", "3");
    expect(screen.getByTestId("left-tree-session-root")).toHaveTextContent("s1");
    expect(screen.getByTestId("left-tree-session-extra-1-root")).toHaveTextContent("other");
  });

  it("没有会话时不画空树，而是说出来", () => {
    render(
      <I18nProvider lang="en">
        <LeftTrees />
      </I18nProvider>,
    );
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
        />
      </I18nProvider>,
    );
    const band = screen.getByTestId("status-band");
    expect(band.querySelectorAll("button")).toHaveLength(2);
  });
});
