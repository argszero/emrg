import { describe, expect, it } from "vitest";
import {
  MEMORY_TAB_ID,
  activate,
  activeTab,
  breadcrumb,
  closeAllFiles,
  closeTab,
  fileLines,
  fileTabCount,
  initialEditor,
  memoryTab,
  openFile,
  pinTab,
  previewIndex,
  type EditorState,
} from "./editor";

/**
 * editor.test.ts — the right panel's tab rules (issue #3068).
 *
 * Every assertion is one of the issue's numbered items, and each is written against
 * the rule rather than against a component: the Memory door cannot be closed, a single
 * click previews and is replaced, a double click pins, closing activates the right-hand
 * neighbour, and one file is one tab however many times it is clicked.
 */

// Pinned on the way in: a single click would be a *preview*, so opening three files that
// way leaves one tab, which is the rule rather than the state these cases need.
const open = (state: EditorState, ...paths: string[]): EditorState =>
  paths.reduce((s, p) => openFile(s, p, { pin: true }), state);

const tabIds = (s: EditorState): string[] => s.tabs.map((t) => t.id);

/** Opening by single click: the preview path the strip actually takes. */
const click = (state: EditorState, ...paths: string[]): EditorState =>
  paths.reduce((s, p) => openFile(s, p), state);

describe("the Memory door", () => {
  it("starts first and alone, and is the active tab", () => {
    const s = initialEditor();
    expect(tabIds(s)).toEqual([MEMORY_TAB_ID]);
    expect(s.activeId).toBe(MEMORY_TAB_ID);
    expect(memoryTab().kind).toBe("memory");
  });

  it("cannot be closed, and refusing changes nothing", () => {
    const before = initialEditor();
    const after = closeTab(before, MEMORY_TAB_ID);
    expect(tabIds(after)).toEqual([MEMORY_TAB_ID]);
    expect(after.activeId).toBe(MEMORY_TAB_ID);
    // Closing every file leaves the door, active — the accepted end state
    const withFiles = open(before, "/r/a.md", "/r/b.md");
    const swept = closeAllFiles(withFiles);
    expect(tabIds(swept)).toEqual([MEMORY_TAB_ID]);
    expect(swept.activeId).toBe(MEMORY_TAB_ID);
  });

  it("stays first after files are opened", () => {
    const s = open(initialEditor(), "/r/a.md");
    expect(tabIds(s)[0]).toBe(MEMORY_TAB_ID);
  });
});

describe("a single click previews, a double click pins", () => {
  it("the first file is a preview (italic), not a pinned tab", () => {
    const s = openFile(initialEditor(), "/r/a.md");
    const file = s.tabs[1];
    expect(file.pinned).toBe(false);
    expect(previewIndex(s.tabs)).toBe(1);
    expect(s.activeId).toBe("/r/a.md");
  });

  it("a second single click replaces the preview instead of adding a tab", () => {
    const s = click(initialEditor(), "/r/a.md", "/r/b.md");
    expect(fileTabCount(s)).toBe(1);
    expect(activeTab(s).path).toBe("/r/b.md");
    expect(activeTab(s).pinned).toBe(false);
  });

  it("a double click keeps the tab, so the next single click adds another", () => {
    const pinned = openFile(initialEditor(), "/r/a.md", { pin: true });
    const s = openFile(pinned, "/r/b.md");
    expect(fileTabCount(s)).toBe(2);
    expect(s.tabs.map((t) => t.pinned)).toEqual([true, true, false]);
  });

  it("one file is one tab: re-opening an open file walks to it", () => {
    const s = click(initialEditor(), "/r/a.md", "/r/b.md");
    const back = openFile(s, "/r/b.md");
    expect(fileTabCount(back)).toBe(1);
    expect(back.activeId).toBe("/r/b.md");
    // Pinned by double click, then clicked again: still one tab, now pinned
    const promoted = openFile(back, "/r/b.md", { pin: true });
    expect(fileTabCount(promoted)).toBe(1);
    expect(activeTab(promoted).pinned).toBe(true);
  });

  it("pinTab promotes in place and leaves a pinned tab alone", () => {
    const s = openFile(initialEditor(), "/r/a.md");
    const pinned = pinTab(s, "/r/a.md");
    expect(pinned.tabs[1].pinned).toBe(true);
    expect(pinTab(pinned, "/r/a.md")).toBe(pinned);
    expect(pinTab(s, "/r/nope")).toBe(s);
  });

  it("the title is the file's base name", () => {
    const s = openFile(initialEditor(), "/p/emrg/README.md");
    expect(s.tabs[1].title).toBe("README.md");
  });
});

describe("closing", () => {
  const four = (): EditorState => open(initialEditor(), "/r/a.md", "/r/b.md", "/r/c.md");

  it("the active tab's right-hand neighbour becomes active", () => {
    const s = four(); // memory, a, b, c — c active
    const onB = activate(s, "/r/b.md");
    const after = closeTab(onB, "/r/b.md");
    expect(tabIds(after)).toEqual([MEMORY_TAB_ID, "/r/a.md", "/r/c.md"]);
    expect(after.activeId).toBe("/r/c.md");
  });

  it("closing the last tab falls back to its left neighbour", () => {
    const s = four(); // c is active and last
    const after = closeTab(s, "/r/c.md");
    expect(after.activeId).toBe("/r/b.md");
  });

  it("closing a tab that is not active leaves the active one alone", () => {
    const s = activate(four(), "/r/a.md");
    const after = closeTab(s, "/r/c.md");
    expect(after.activeId).toBe("/r/a.md");
    expect(fileTabCount(after)).toBe(2);
  });

  it("closing every tab returns to the memory door", () => {
    let s = four();
    for (const id of ["/r/a.md", "/r/b.md", "/r/c.md"]) s = closeTab(s, id);
    expect(tabIds(s)).toEqual([MEMORY_TAB_ID]);
    expect(s.activeId).toBe(MEMORY_TAB_ID);
  });

  it("an unknown id changes nothing", () => {
    const s = four();
    expect(closeTab(s, "/r/zzz")).toBe(s);
    expect(activate(s, "/r/zzz")).toBe(s);
  });
});

describe("breadcrumb", () => {
  it("names the root the file sits under, then the way down", () => {
    const b = breadcrumb("/p/emrg/.emrg/sessions/s1/history.jsonl", "/p/emrg/.emrg/sessions/s1");
    expect(b.root).toBe("/p/emrg/.emrg/sessions/s1");
    expect(b.parts).toEqual(["history.jsonl"]);
  });

  it("keeps the whole path as segments when no root is known or the root does not contain it", () => {
    expect(breadcrumb("/p/a/b.md", null).root).toBeNull();
    expect(breadcrumb("/p/a/b.md", null).parts).toEqual(["p", "a", "b.md"]);
    const other = breadcrumb("/p/other/b.md", "/p/emrg");
    expect(other.root).toBeNull();
    expect(other.parts).toEqual(["p", "other", "b.md"]);
  });

  it("a trailing separator on the root is the same root", () => {
    const b = breadcrumb("/p/emrg/a.md", "/p/emrg/");
    expect(b.root).toBe("/p/emrg");
    expect(b.parts).toEqual(["a.md"]);
  });
});

describe("fileLines", () => {
  it("one entry per rendered row, and a trailing newline ends the last line", () => {
    expect(fileLines("a\nb")).toEqual(["a", "b"]);
    expect(fileLines("a\nb\n")).toEqual(["a", "b", ""]);
    expect(fileLines("")).toEqual([""]);
  });
});
