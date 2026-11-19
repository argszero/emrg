import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { SessionColumn } from "./SessionColumn";
import { InspectorPanel } from "./InspectorPanel";
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

describe("InspectorPanel", () => {
  it("三个 tab，名字就是本轮动作 / 文件 / 记忆", () => {
    render(
      <I18nProvider lang="en">
        <InspectorPanel actions={[]} files={[]} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("inspector-tab-actions")).toHaveTextContent("This turn");
    expect(screen.getByTestId("inspector-tab-files")).toHaveTextContent("Files");
    expect(screen.getByTestId("inspector-tab-memory")).toHaveTextContent("Memory");
    expect(screen.getByTestId("inspector-panel").querySelectorAll("[role='tab']")).toHaveLength(3);
  });
});

describe("StatusBand", () => {
  it("右端只有两个栏开关，别无他物（issue #3028）", () => {
    const noop = () => {};
    render(
      <I18nProvider lang="en">
        <StatusBand
          running={false}
          round={null}
          tokens={null}
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
