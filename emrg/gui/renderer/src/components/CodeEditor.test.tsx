import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  // The keys the platform sends as themselves — Enter, Backspace — with no modifier, which
  // is the only way `insertNewlineAndIndent` and `deleteBracketPair` are ever reached.
  const pressPlain = (key: string): void => {
    fireEvent.keyDown(content(), { key });
  };
  return {
    ref,
    onChange,
    onSave,
    onCursor,
    surface,
    editor,
    content,
    press,
    pressPlain,
    ...view,
  };
}

/**
 * Type one character the way the browser does.
 *
 * jsdom mutates nothing for a keystroke, so the DOM insert that a real key triggers never
 * happens here — and CodeMirror's `inputHandler` facet is exactly what the view calls when
 * it does. Calling the facet directly is the closest a jsdom test gets to a real key: the
 * pairing rule that answers is CodeMirror's own, not a model of it.
 */
function typeText(editor: EditorView, text: string): void {
  const main = editor.state.selection.main;
  const insert = (from: number, to: number, content: string) =>
    editor.state.update({
      changes: { from, to, insert: content },
      selection: { anchor: from + content.length },
      userEvent: "input.type",
    });
  for (const handler of editor.state.facet(EditorView.inputHandler)) {
    const transaction = handler(editor, main.from, main.to, text, insert);
    if (transaction) {
      editor.dispatch(transaction);
      return;
    }
  }
  editor.dispatch(insert(main.from, main.to, text));
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

/**
 * Item 6: the find panel has to answer "how many, and which one am I on".
 *
 * CodeMirror's panel gives the field, the case toggle and the arrows, and no count at all —
 * so this is the one item of the rant that needed code as well as a test, and the arm that
 * deletes the count has nothing else to redden.
 */
describe("CodeEditor find panel (item 6)", () => {
  const setQuery = (value: string): void => {
    const field = document.querySelector<HTMLInputElement>('input[name="search"]');
    if (!field) throw new Error("the find field is not on screen");
    fireEvent.change(field, { target: { value } });
  };
  const count = (): string => screen.getByTestId("editor-search-count").textContent ?? "";

  it("显示命中总数与当前位置，光标一动就跟着走", async () => {
    const { press, editor } = mountEditor({ text: "foo bar foo baz foo", uiLang: "zh" });
    press("f");
    // The count goes into CodeMirror's panel, and the panel's DOM is written after the
    // update that opens it — so the first number of a fresh panel arrives a microtask late.
    await waitFor(() => expect(count()).toBe(""));

    setQuery("foo");
    expect(count()).toBe("1 / 3");
    act(() => {
      editor().dispatch({ selection: { anchor: 8 } });
    });
    expect(count()).toBe("2 / 3");
    act(() => {
      editor().dispatch({ selection: { anchor: 16 } });
    });
    expect(count()).toBe("3 / 3");
  });

  it("一处也没有时是 0 / 0，不是一个空框", async () => {
    const { press } = mountEditor({ text: "foo bar", uiLang: "zh" });
    press("f");
    await waitFor(() => expect(count()).toBe(""));
    setQuery("zzz");
    expect(count()).toBe("0 / 0");
  });

  it("区分大小写开关一按，命中总数跟着变", () => {
    const { press } = mountEditor({ text: "foo Foo foo", uiLang: "zh" });
    press("f");
    setQuery("foo");
    expect(count()).toBe("1 / 3");

    const box = document.querySelector<HTMLInputElement>('input[name="case"]');
    if (!box) throw new Error("the case toggle is not on screen");
    box.checked = true;
    fireEvent.change(box);
    expect(count()).toBe("1 / 2");
  });

  it("下一个把选区挪到下一处命中，不是原地不动", () => {
    const { press, editor, surface } = mountEditor({ text: "foo bar foo baz foo", uiLang: "zh" });
    press("f");
    setQuery("foo");
    const next = surface().querySelector<HTMLButtonElement>('button[name="next"]');
    if (!next) throw new Error("the next button is not on screen");

    fireEvent.click(next);
    const first = editor().state.selection.main;
    expect(editor().state.doc.sliceString(first.from, first.to)).toBe("foo");
    fireEvent.click(next);
    const second = editor().state.selection.main;
    expect(second.from).not.toBe(first.from);
    expect(editor().state.doc.sliceString(second.from, second.to)).toBe("foo");
  });

  it("Cmd+G 打开跳到行，填一个行号提交就跳过去", async () => {
    const { press, editor, surface } = mountEditor({ text: "one\ntwo\nthree", uiLang: "zh" });
    press("g");
    const dialog = surface().querySelector<HTMLElement>(".cm-dialog");
    expect(dialog).not.toBeNull();
    // 对话框的标题是「跳到行」，输入框是它的 line 字段（label 不是 placeholder）
    expect(dialog).toHaveTextContent("跳到行");
    const field = dialog?.querySelector<HTMLInputElement>('input[name="line"]');
    expect(field).not.toBeNull();

    fireEvent.change(field as HTMLInputElement, { target: { value: "3" } });
    fireEvent.submit(dialog?.querySelector("form") as HTMLFormElement);
    await waitFor(() =>
      expect(editor().state.doc.lineAt(editor().state.selection.main.head).number).toBe(3),
    );
  });
});

/** Item 9: the rules that make typing feel like an editor rather than a text box. */
describe("CodeEditor editing rules (item 9)", () => {
  it("打一个左括号，右括号自己补上，光标落在中间", () => {
    const { editor } = mountEditor({ text: "" });
    act(() => {
      typeText(editor(), "(");
    });
    expect(editor().state.doc.toString()).toBe("()");
    expect(editor().state.selection.main.head).toBe(1);
  });

  it("引号也成对补上，同一个键再打一次是穿过去而不是又补一个", () => {
    const { editor } = mountEditor({ text: "" });
    act(() => {
      typeText(editor(), '"');
    });
    expect(editor().state.doc.toString()).toBe('""');
    act(() => {
      typeText(editor(), '"');
    });
    expect(editor().state.doc.toString()).toBe('""');
    expect(editor().state.selection.main.head).toBe(2);
  });

  it("Backspace 在两个半括号之间把它们一起删掉", () => {
    const { editor, pressPlain } = mountEditor({ text: "" });
    act(() => {
      typeText(editor(), "(");
    });
    expect(editor().state.doc.toString()).toBe("()");
    pressPlain("Backspace");
    expect(editor().state.doc.toString()).toBe("");
  });

  it("回车按语言缩进：Python 的块里回车自动进一层", async () => {
    const { editor, pressPlain } = mountEditor({
      text: "if True:\n    pass",
      language: { name: "Python" },
    });
    // 语言支持是异步装上的：没有它，回车就没有缩进规则可问。语言数据是它真装上的证据 ——
    // 只等一个字段身份，会等到一份装错的扩展也照样绿的假象。
    await waitFor(() =>
      expect(editor().state.languageDataAt("commentTokens", 0).length).toBeGreaterThan(0),
    );
    act(() => {
      editor().dispatch({ selection: { anchor: editor().state.doc.length } });
    });
    pressPlain("Enter");
    const newLine = editor().state.doc.line(3).text;
    // 进了一层：整行是缩进，不是顶格。具体几个空格是 CodeMirror 的缩进单位（默认 2），
    // 这里问的是「进了没有」，不是「进了几个」。
    expect(newLine).toMatch(/^ +$/);
    expect(newLine.length).toBeGreaterThanOrEqual(2);
  });
});

/**
 * Item 11: an input method has to be able to work here.
 *
 * jsdom cannot compose — there is no candidate window and no platform IME — so what is
 * checked is what the composition path rests on: a real editing host for the IME to attach
 * to, and one composed insertion that undo takes back whole. Character-by-character undo of
 * a composed word is what "撤销吞字" looks like in practice.
 */
describe("CodeEditor input method (item 11)", () => {
  it("编辑面是真正的可编辑宿主，输入法才有地方挂候选框", () => {
    const { content } = mountEditor();
    expect(content()).toHaveAttribute("contenteditable", "true");
  });

  it("一次输入的中文是一个整体：撤销一次整段回去，不留半个字", () => {
    const { editor, press } = mountEditor({ text: "alpha" });
    act(() => {
      typeText(editor(), "中文");
    });
    expect(editor().state.doc.toString()).toBe("中文alpha");
    press("z");
    expect(editor().state.doc.toString()).toBe("alpha");
  });
});
