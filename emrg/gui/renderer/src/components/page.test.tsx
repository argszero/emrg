import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { SessionColumn } from "./SessionColumn";
import { EditorPanel } from "./EditorPanel";
import { initialEditor } from "../lib/editor";
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

describe("the right section", () => {
  it("is the editor, and its first tab is the memory door (issue #3068)", () => {
    // What the editor does with a tab — preview, pin, close, scroll, breadcrumb — is
    // EditorPanel.test.tsx's subject; what belongs here is that the page's fourth
    // section is the editor and that it opens on the door, not on a directory.
    render(
      <I18nProvider lang="en">
        <EditorPanel state={initialEditor()} onChange={() => {}} />
      </I18nProvider>,
    );
    const panel = screen.getByTestId("editor-panel");
    const tabs = panel.querySelectorAll("[role='tab']");
    expect(tabs).toHaveLength(1);
    expect(tabs[0]).toHaveAttribute("data-kind", "memory");
    expect(panel.textContent).not.toContain("Agent dir");
    expect(panel.textContent).not.toContain("Session dir");
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
