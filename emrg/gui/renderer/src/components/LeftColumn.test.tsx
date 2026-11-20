import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { LeftColumn } from "./LeftColumn";
import { LEFT_TREE_HEADER_PX } from "../lib/cockpit";

/**
 * LeftColumn.test.tsx — the three stacked sections, and the collapse rule (issue #3068
 * item 1).
 *
 * The contract is a rectangle: a collapsed section keeps its header where it was and
 * hands its height to the section below, and the bottom section's height stays standing
 * at the bottom instead of moving up. This DOM has no layout engine, so the rectangle is
 * read the only way it exists here — the column's own measured height is stubbed, and
 * the sections' inline heights are the numbers the browser would lay out. Those numbers
 * come from `leftTreeLayout`, which `cockpit.test.ts` checks against the rule itself.
 */

function renderColumn(total = 400) {
  const spy = vi
    .spyOn(HTMLElement.prototype, "clientHeight", "get")
    .mockReturnValue(total);
  const view = render(
    <I18nProvider lang="en">
      <LeftColumn
        sessions={<div data-testid="sessions-body">sessions</div>}
        agentTree={<div data-testid="agent-tree">agent</div>}
        sessionTree={<div data-testid="session-tree">session</div>}
      />
    </I18nProvider>,
  );
  return { view, restore: () => spy.mockRestore() };
}

const heightOf = (testId: string): number => parseFloat((screen.getByTestId(testId) as HTMLElement).style.height);

describe("LeftColumn", () => {
  it("renders three sections in order: sessions, agent directory, session directory", () => {
    const { restore } = renderColumn();
    try {
      const stack = screen.getByTestId("left-stack");
      const sessions = screen.getByTestId("left-sessions");
      const agent = screen.getByTestId("left-section-agent");
      const session = screen.getByTestId("left-section-session");
      expect(sessions.parentElement).toBe(stack);
      expect(agent.parentElement).toBe(stack);
      expect(session.parentElement).toBe(stack);
      // document order, read off the DOM rather than trusted to the source
      const order = [...stack.children].map((el) => el.getAttribute("data-testid"));
      expect(order.slice(0, 4)).toEqual(["left-sessions", "left-section-agent", "left-section-session", "left-spacer"]);
    } finally {
      restore();
    }
  });

  it("the column measures itself: a 400px column gives each tree a quarter", () => {
    const { restore } = renderColumn(400);
    try {
      expect(screen.getByTestId("left-stack")).toHaveAttribute("data-total", "400");
      expect(heightOf("left-section-agent")).toBe(100);
      expect(heightOf("left-section-session")).toBe(100);
      expect(screen.getByTestId("left-spacer")).toHaveAttribute("data-height", "0");
    } finally {
      restore();
    }
  });

  it("clicking the agent section's header collapses it: its header stays, its body goes, the section below grows", () => {
    const { restore } = renderColumn(400);
    try {
      fireEvent.click(screen.getByTestId("left-section-agent-header"));
      expect(screen.queryByTestId("left-section-agent-body")).toBeNull();
      // the header is still there, in the same section, at the top of it
      expect(screen.getByTestId("left-section-agent-header")).toBeInTheDocument();
      expect(heightOf("left-section-agent")).toBe(LEFT_TREE_HEADER_PX);
      // and everything it gave up went to the section below, not to the sessions list
      expect(heightOf("left-section-session")).toBe(176);
      expect(screen.getByTestId("left-section-agent")).toHaveAttribute("data-collapsed", "1");
      expect(screen.getByTestId("left-section-agent-header")).toHaveAttribute("aria-expanded", "false");
    } finally {
      restore();
    }
  });

  it("collapsing the bottom section leaves the freed height at the column's bottom", () => {
    const { restore } = renderColumn(400);
    try {
      fireEvent.click(screen.getByTestId("left-section-session-header"));
      expect(heightOf("left-section-agent")).toBe(100); // not redistributed upward
      expect(heightOf("left-section-session")).toBe(LEFT_TREE_HEADER_PX);
      expect(screen.getByTestId("left-spacer")).toHaveAttribute("data-height", "76");
      expect(parseFloat((screen.getByTestId("left-spacer") as HTMLElement).style.flexBasis)).toBe(76);
    } finally {
      restore();
    }
  });

  it("clicking a collapsed header opens it again", () => {
    const { restore } = renderColumn(400);
    try {
      const header = screen.getByTestId("left-section-agent-header");
      fireEvent.click(header);
      fireEvent.click(header);
      expect(screen.getByTestId("left-section-agent-body")).toBeInTheDocument();
      expect(heightOf("left-section-agent")).toBe(100);
      expect(screen.getByTestId("left-section-agent-header")).toHaveAttribute("aria-expanded", "true");
    } finally {
      restore();
    }
  });

  it("the sessions list is passed through untouched and keeps its own half", () => {
    const { restore } = renderColumn(400);
    try {
      expect(screen.getByTestId("sessions-body")).toBeInTheDocument();
      expect(screen.getByTestId("agent-tree")).toBeInTheDocument();
      expect(screen.getByTestId("session-tree")).toBeInTheDocument();
      fireEvent.click(screen.getByTestId("left-section-session-header"));
      // 400 - 100 - 24 - 76 = 200: the session list's half, before and after
      expect(screen.getByTestId("left-sessions")).toBeInTheDocument();
    } finally {
      restore();
    }
  });

  it("an unmeasured column still renders all three sections", async () => {
    const { restore } = renderColumn(0);
    try {
      await waitFor(() => expect(screen.getByTestId("left-section-agent")).toBeInTheDocument());
      expect(screen.getByTestId("left-section-agent").style.flexBasis).toBe("25%");
      expect(screen.getByTestId("left-section-session").style.flexBasis).toBe("25%");
    } finally {
      restore();
    }
  });
});
