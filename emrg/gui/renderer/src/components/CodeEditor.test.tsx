import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { EditorView } from "@codemirror/view";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";
import { PLAIN_TEXT, type CursorPosition } from "../lib/editorDoc";

/**
 * CodeEditor — the editing surface and its key bindings (rant 2026-11-20T18:01:52).
 *
 * The text is driven through a real CodeMirror transaction (`EditorView.findFromDOM`), not
 * through a stubbed component: a stub would prove the wiring and prove nothing about
 * whether the editor accepts an edit. `findFromDOM` is CodeMirror's own lookup, so what
 * these tests reach is the live view the host types into.
 */

function mountEditor(props: Partial<Parameters<typeof CodeEditor>[0]> = {}) {
  const ref = createRef<CodeEditorHandle>();
  const onChange = vi.fn();
  const onSave = vi.fn();
  const onCursor = vi.fn<(position: CursorPosition) => void>();
  const view = render(
    <CodeEditor
      ref={ref}
      text={"alpha\nbeta"}
      language={PLAIN_TEXT}
      readOnly={false}
      wrap={false}
      onChange={onChange}
      onSave={onSave}
      onCursor={onCursor}
      {...props}
    />,
  );
  const surface = (): HTMLElement => screen.getByTestId("code-editor");
  const editor = (): EditorView => {
    const dom = surface().querySelector(".cm-editor") as HTMLElement | null;
    const found = dom ? EditorView.findFromDOM(dom) : null;
    if (!found) throw new Error("the editor did not mount a view");
    return found;
  };
  // The element CodeMirror binds its keymap to.
  const content = (): HTMLElement => editor().contentDOM;
  const press = (key: string, mods: Record<string, boolean> = {}): void => {
    // `ctrlKey` alone, not both modifiers: CodeMirror resolves `Mod` once per platform
    // (meta on a Mac, ctrl elsewhere) and folds whatever it sees into the key's name, so a
    // press carrying both arrives as `Ctrl-Meta-s` and matches no binding at all. jsdom
    // reports no platform, so `Mod` is Ctrl here — which is why this is the modifier sent.
    fireEvent.keyDown(content(), { key, ctrlKey: true, ...mods });
  };
  return { ref, onChange, onSave, onCursor, surface, editor, content, press, ...view };
}

describe("CodeEditor surface", () => {
  it("文本进了编辑器，语言与只读状态挂在它自己的元素上", () => {
    mountEditor({ language: { name: "Python" } });
    const surface = screen.getByTestId("code-editor");
    expect(surface).toHaveTextContent("alpha");
    expect(surface).toHaveAttribute("data-language", "Python");
    expect(surface).toHaveAttribute("data-readonly", "false");
    expect(surface).toHaveAttribute("data-wrap", "false");
  });

  it("纯文本的语言名不是硬编码的英文，而是 plain", () => {
    mountEditor();
    expect(screen.getByTestId("code-editor")).toHaveAttribute("data-language", "plain");
  });

  it("打一个字就是一次改动：onChange 拿到改完的全文", () => {
    const { onChange, editor } = mountEditor();
    act(() => {
      editor().dispatch({ changes: { from: 0, insert: "X" } });
    });
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("Xalpha\nbeta");
  });

  it("只读时编辑器不接受改动，光标也进不去", () => {
    const { editor } = mountEditor({ readOnly: true });
    expect(screen.getByTestId("code-editor")).toHaveAttribute("data-readonly", "true");
    // 只读是两半：state 拒用户输入（readOnly 为真），view 拒光标（contenteditable 为假）。
    // 程序化 dispatch 仍会生效 —— 那是面板自己的刷新通道，不是用户在打字，两件事。
    expect(editor().state.readOnly).toBe(true);
    expect(editor().contentDOM).toHaveAttribute("contenteditable", "false");
  });
});

describe("CodeEditor key bindings (item 6, item 7)", () => {
  it("Cmd+S 交给面板保存，不让平台拿走这个键", () => {
    const { onSave, press } = mountEditor();
    press("s");
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it("Cmd+F 打开查找框，框名是中文", () => {
    const { press, surface } = mountEditor({ uiLang: "zh" });
    press("f");
    const panel = surface().querySelector(".cm-panel.cm-search");
    expect(panel).not.toBeNull();
    // 「查找」是输入框的 placeholder，不是面板里的文字：CodeMirror 把标签放在占位符上
    expect(panel?.querySelector<HTMLInputElement>('input[name="search"]')?.placeholder).toBe(
      "查找",
    );
    // 命中数、上一个 / 下一个、区分大小写都在面板上
    expect(panel).toHaveTextContent("下一个");
    expect(panel).toHaveTextContent("上一个");
    expect(panel).toHaveTextContent("区分大小写");
  });

  it("Cmd+Option+F 打开查找框并把光标放进替换框", () => {
    const { press, surface } = mountEditor({ uiLang: "zh" });
    press("f", { altKey: true });
    const panel = surface().querySelector(".cm-panel.cm-search");
    expect(panel).not.toBeNull();
    expect(panel).toHaveTextContent("全部替换");
    // 同步的 microtask 之后焦点在替换输入框里
    return Promise.resolve().then(() => {
      const replace = surface().querySelector<HTMLInputElement>('input[name="replace"]');
      expect(replace).not.toBeNull();
      expect(document.activeElement).toBe(replace);
    });
  });

  it("英文界面下查找框是英文：两个语言不混", () => {
    const { press, surface } = mountEditor({ uiLang: "en" });
    press("f");
    const panel = surface().querySelector(".cm-panel.cm-search");
    expect(panel).not.toBeNull();
    expect(panel?.querySelector<HTMLInputElement>('input[name="search"]')?.placeholder).toBe(
      "Find",
    );
    expect(panel).toHaveTextContent("match case");
    expect(panel).not.toHaveTextContent("区分大小写");
  });

  it("Cmd+D 把下一个相同词也选中", () => {
    const { editor, press } = mountEditor({ text: "foo bar foo" });
    act(() => {
      // `{anchor, head}`, not `{anchor, to}`: a spec with `to` is not a selection CodeMirror
      // reads, and it degrades to a bare cursor — which sends the command down its
      // "select the word I am in" branch and leaves one range, looking like a bug in it.
      editor().dispatch({ selection: { anchor: 0, head: 3 } });
    });
    expect(editor().state.selection.ranges).toHaveLength(1);
    press("d");
    expect(editor().state.selection.ranges).toHaveLength(2);
    expect(editor().state.selection.ranges[1].from).toBe(8);
    expect(editor().state.selection.ranges[1].to).toBe(11);
  });

  it("jumpToLine 把光标放到那一行", () => {
    const { ref, editor } = mountEditor({ text: "one\ntwo\nthree" });
    act(() => {
      ref.current?.jumpToLine(3);
    });
    expect(editor().state.doc.lineAt(editor().state.selection.main.head).number).toBe(3);
    // 超出末行时夹到末行，不是把光标丢到文档外
    act(() => {
      ref.current?.jumpToLine(99);
    });
    expect(editor().state.doc.lineAt(editor().state.selection.main.head).number).toBe(3);
  });

  it("scrollTop 读得出也写得回：静默重载靠它把读者放回原处", () => {
    const { ref, editor } = mountEditor();
    const scroller = editor().scrollDOM;
    act(() => {
      ref.current?.setScrollTop(140);
    });
    expect(scroller.scrollTop).toBe(140);
    expect(ref.current?.scrollTop()).toBe(140);
    act(() => {
      ref.current?.setScrollTop(-5);
    });
    expect(scroller.scrollTop).toBe(0);
  });
});

describe("CodeEditor props follow changes without remounting", () => {
  it("text 换了一份就换掉整个文档", () => {
    const { rerender, editor } = mountEditor();
    rerender(
      <CodeEditor text={"replaced"} language={PLAIN_TEXT} readOnly={false} wrap={false} />,
    );
    expect(editor().state.doc.toString()).toBe("replaced");
  });

  it("折行开关切换不重建编辑器，只改折行扩展", () => {
    const { rerender, editor } = mountEditor();
    const before = editor();
    rerender(<CodeEditor text={"alpha\nbeta"} language={PLAIN_TEXT} readOnly={false} wrap />);
    expect(editor()).toBe(before);
    expect(screen.getByTestId("code-editor")).toHaveAttribute("data-wrap", "true");
  });
});
