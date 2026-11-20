import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { I18nProvider } from "../lib/i18n";
import { EditorPanel, type FileContent } from "./EditorPanel";
import { initialEditor, openFile, type EditorState } from "../lib/editor";

/**
 * EditorPanel.test.tsx — the right panel as an editor, item by item (issue #3068).
 *
 * The tab rules themselves are `lib/editor.test.ts`'s subject; what is asserted here is
 * what the host actually sees: which tab is first and which has no ✕, that a click
 * opens a tab and the breadcrumb names the file's full path, that closing the active tab
 * moves right, that each tab comes back to its own scroll position, and that no number
 * of tabs may widen the panel.
 */

const file = (content: string, over: Partial<FileContent> = {}): FileContent => ({
  content,
  binary: false,
  truncated: false,
  ...over,
});

interface HarnessProps {
  start?: EditorState;
  readFile?: (p: { path: string }) => Promise<FileContent>;
  rootOf?: (p: string) => string | null;
  onOpenMemory?: () => void;
}

/**
 * A panel with its state held by the test: opening is driven by buttons rather than by
 * the tree, so each case starts from a state the test wrote down.
 */
function Harness({ start, readFile, rootOf, onOpenMemory = () => {} }: HarnessProps) {
  const [state, setState] = useState<EditorState>(start ?? initialEditor());
  return (
    <I18nProvider lang="en">
      <button type="button" data-testid="open-a" onClick={() => setState((s) => openFile(s, "/p/emrg/a.md"))}>
        a
      </button>
      <button type="button" data-testid="open-b" onClick={() => setState((s) => openFile(s, "/p/emrg/b.md"))}>
        b
      </button>
      <button
        type="button"
        data-testid="open-a-pinned"
        onClick={() => setState((s) => openFile(s, "/p/emrg/a.md", { pin: true }))}
      >
        a!
      </button>
      <EditorPanel
        state={state}
        onChange={setState}
        readFile={readFile ?? (async () => file("hello"))}
        rootOf={rootOf ?? (() => "/p/emrg")}
        onOpenMemory={onOpenMemory}
      />
    </I18nProvider>
  );
}

const tabs = (): HTMLElement[] => screen.queryAllByTestId("editor-tab");
const activeTabId = (): string | null =>
  tabs().find((t) => t.getAttribute("data-active") === "1")?.getAttribute("data-tab-id") ?? null;

describe("the strip (items 2 and 4)", () => {
  it("the first tab is Memory, it has no close button, and no directory tab exists", () => {
    render(<Harness />);
    const all = tabs();
    expect(all).toHaveLength(1);
    expect(all[0]).toHaveAttribute("data-kind", "memory");
    expect(screen.getAllByTestId("editor-tab-name")[0]).toHaveTextContent("Memory");
    expect(screen.queryAllByTestId("editor-tab-close")).toHaveLength(0);
    // the two tabs this panel used to have are gone
    expect(screen.queryByText("Agent dir")).toBeNull();
    expect(screen.queryByText("Session dir")).toBeNull();
  });

  it("a file tab is a preview in italics until it is pinned", async () => {
    render(<Harness readFile={async () => file("x")} />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    const fileTab = tabs()[1];
    expect(fileTab).toHaveAttribute("data-preview", "1");
    expect(fileTab.className).toContain("preview");
    expect(fileTab).toHaveAttribute("data-shrink", "0");

    fireEvent.doubleClick(fileTab);
    await waitFor(() => expect(tabs()[1]).toHaveAttribute("data-preview", "0"));
    expect(tabs()[1].className).not.toContain("preview");
  });

  it("a single click on another file replaces the preview", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent.click(screen.getByTestId("open-b"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    expect(activeTabId()).toBe("/p/emrg/b.md");
    expect(tabs()[1]).toHaveAttribute("data-tab-id", "/p/emrg/b.md");
  });

  it("a pinned tab is kept, so the next single click opens a second one", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent.click(screen.getByTestId("open-b"));
    await waitFor(() => expect(tabs()).toHaveLength(3));
    expect(tabs()[1]).toHaveAttribute("data-tab-id", "/p/emrg/a.md");
    expect(tabs()[2]).toHaveAttribute("data-tab-id", "/p/emrg/b.md");
  });

  it("clicking a tab switches to it", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent.click(screen.getByTestId("open-b"));
    await waitFor(() => expect(tabs()).toHaveLength(3));
    fireEvent.click(tabs()[1]);
    await waitFor(() => expect(activeTabId()).toBe("/p/emrg/a.md"));
  });
});

describe("the content (items 3, 5 and 9)", () => {
  it("the breadcrumb names the root and the full path, and the code area numbers its lines", async () => {
    render(<Harness readFile={async () => file("one\ntwo\nthree")} />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(screen.getByTestId("editor-code")).toBeInTheDocument());

    expect(screen.getByTestId("editor-breadcrumb-root")).toHaveTextContent("/p/emrg");
    expect(screen.getByTestId("editor-breadcrumb").textContent).toBe("/p/emrg/a.md");

    expect(screen.getAllByTestId("editor-line")).toHaveLength(3);
    expect(screen.getAllByTestId("editor-line")[1]).toHaveTextContent("two");
    // the gutter is its own column: three numbers for three rows
    const gutter = screen.getByTestId("editor-gutter");
    expect(gutter.textContent).toBe("123");
  });

  it("closing the active tab activates its right-hand neighbour", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent.click(screen.getByTestId("open-b"));
    await waitFor(() => expect(tabs()).toHaveLength(3));
    // b is the active preview, last in the strip; pin it so closing a is not a preview swap
    fireEvent.doubleClick(tabs()[2]);
    fireEvent.click(tabs()[1]); // a is active, b sits to its right
    await waitFor(() => expect(activeTabId()).toBe("/p/emrg/a.md"));

    const closes = screen.getAllByTestId("editor-tab-close");
    expect(closes).toHaveLength(2);
    fireEvent.click(closes[0]); // close a (the left-hand file tab)
    await waitFor(() => expect(tabs()).toHaveLength(2));
    expect(activeTabId()).toBe("/p/emrg/b.md");
  });

  it("closing every file leaves only Memory, with the door and no breadcrumb or code", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent.click(screen.getAllByTestId("editor-tab-close")[0]);
    await waitFor(() => expect(tabs()).toHaveLength(1));
    expect(tabs()[0]).toHaveAttribute("data-kind", "memory");
    expect(screen.getByTestId("editor-memory")).toBeInTheDocument();
    expect(screen.getByTestId("editor-memory-open")).toBeInTheDocument();
    expect(screen.queryByTestId("editor-breadcrumb")).toBeNull();
    expect(screen.queryByTestId("editor-code")).toBeNull();
  });

  it("a middle click closes a tab", async () => {
    render(<Harness />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    fireEvent(tabs()[1], new MouseEvent("auxclick", { bubbles: true, button: 1 }));
    await waitFor(() => expect(tabs()).toHaveLength(1));
  });

  it("the memory door's button still opens the memory panel", () => {
    const openMemory = vi.fn();
    render(<Harness onOpenMemory={openMemory} />);
    fireEvent.click(screen.getByTestId("editor-memory-open"));
    expect(openMemory).toHaveBeenCalledTimes(1);
  });

  it("a binary file, a truncated read and a failure each say so instead of drawing nothing", async () => {
    const { unmount } = render(<Harness readFile={async () => file("", { binary: true })} />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(screen.getByTestId("editor-binary")).toBeInTheDocument());
    unmount();

    const two = render(<Harness readFile={async () => file("a\nb", { truncated: true })} />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(screen.getByTestId("editor-truncated")).toBeInTheDocument());
    two.unmount();

    render(<Harness readFile={async () => Promise.reject(new Error("no such file: /p/emrg/a.md"))} />);
    fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(screen.getByTestId("editor-error")).toHaveTextContent("no such file"));
  });
});

describe("tab memory and width (items 6 and 7)", () => {
  it("each tab comes back to its own scroll position", async () => {
    render(<Harness readFile={async () => file(Array.from({ length: 200 }, (_, i) => `line ${i + 1}`).join("\n"))} />);
    fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(screen.getByTestId("editor-code")).toBeInTheDocument());
    const code = screen.getByTestId("editor-code");
    code.scrollTop = 240;
    fireEvent.scroll(code);

    fireEvent.click(screen.getByTestId("open-b"));
    await waitFor(() => expect(activeTabId()).toBe("/p/emrg/b.md"));
    expect((screen.getByTestId("editor-code") as HTMLElement).scrollTop).toBe(0);

    fireEvent.click(tabs()[1]); // back to a
    await waitFor(() => expect(activeTabId()).toBe("/p/emrg/a.md"));
    expect((screen.getByTestId("editor-code") as HTMLElement).scrollTop).toBe(240);
  });

  it("no number of tabs widens the panel: the strip scrolls itself", async () => {
    render(<Harness />);
    const panel = screen.getByTestId("editor-panel");
    const before = panel.getAttribute("style");
    for (let i = 0; i < 12; i++) fireEvent.click(screen.getByTestId("open-a-pinned"));
    await waitFor(() => expect(tabs().length).toBeGreaterThanOrEqual(2));

    // The panel claims no width at all — the column around it does — so nothing about
    // the strip can move it. The strip says it scrolls, and its tabs do not shrink.
    expect(panel.getAttribute("style")).toBe(before);
    expect(panel.getAttribute("style")).toBeNull();
    expect(screen.getByTestId("editor-strip")).toHaveAttribute("data-scrolls", "x");
    for (const t of tabs()) expect(t).toHaveAttribute("data-shrink", "0");
  });

  it("the same file is one tab however many times it is clicked", async () => {
    render(<Harness />);
    for (let i = 0; i < 4; i++) fireEvent.click(screen.getByTestId("open-a"));
    await waitFor(() => expect(tabs()).toHaveLength(2));
    expect(tabs()[1]).toHaveAttribute("data-tab-id", "/p/emrg/a.md");
  });
});
