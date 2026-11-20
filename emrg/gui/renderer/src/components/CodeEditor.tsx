import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import { Compartment, EditorSelection, EditorState, type Extension } from "@codemirror/state";
import {
  EditorView,
  crosshairCursor,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  highlightSpecialChars,
  keymap,
  lineNumbers,
  rectangularSelection,
  ViewPlugin,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  HighlightStyle,
  bracketMatching,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import {
  getSearchQuery,
  gotoLine,
  highlightSelectionMatches,
  openSearchPanel,
  searchKeymap,
  selectNextOccurrence,
} from "@codemirror/search";
import { tags } from "@lezer/highlight";
import {
  cursorPositionAt,
  languageSupportFor,
  type CursorPosition,
  type LanguageChoice,
} from "../lib/editorDoc";

/**
 * CodeEditor — the editable surface of the right column (rant 2026-11-20T18:01:52).
 *
 * This is CodeMirror 6 doing the editing, not a textarea with opinions: caret, selection,
 * multiple selections, undo/redo, bracket pairing, auto-indent, search and replace, and an
 * input method that inserts into the document rather than into a parallel buffer are all
 * things a hand-written editor gets wrong in ways that only show up under a person's
 * hands. What belongs *here* is the wiring — which key does what, what the buffer reports
 * as it changes, and which of its extensions are switched on.
 *
 * The bindings are the platform's, and each answers one item of the rant:
 *
 * - `Mod-s` saves — the panel owns the write, so this only calls it;
 * - `Mod-f` finds; `Mod-Alt-f` finds **and replaces** (CodeMirror's panel carries a replace
 *   field, and opening it is not the same as putting the caret in it, which is the whole
 *   difference between the two keys);
 * - `Mod-g` goes to a line;
 * - `Mod-d` selects the next occurrence of the selection — the multi-cursor a person
 *   actually uses;
 * - `Alt`-click adds a caret at the click, and `Alt`-drag is a rectangular selection, both
 *   of which CodeMirror already implements once asked;
 * - `Mod-z` / `Mod-Shift-z` undo and redo, from the history extension.
 *
 * The panel and this component talk in both directions: `onChange` for the buffer,
 * `onCursor` for the status corner. A remount per tab (the panel keys on the path) is why
 * the initial caret and scroll arrive as props — a tab that comes back should look like
 * the one that was left, not like a freshly opened file.
 */
export interface CodeEditorProps {
  /** The buffer to edit. A different string from the last one **replaces the document**. */
  text: string;
  language: LanguageChoice;
  readOnly: boolean;
  /** Soft-wrap long lines. A per-session choice, so it arrives as a prop. */
  wrap: boolean;
  onCursor?: (position: CursorPosition) => void;
  onChange?: (text: string) => void;
  /** `Mod-s`. Called instead of letting the platform have the key. */
  onSave?: () => void;
  /** Where to scroll to on mount, so a returned tab keeps its place. */
  initialScrollTop?: number;
  onScrollTop?: (top: number) => void;
  /** The interface language, for CodeMirror's own search panel labels. */
  uiLang?: string;
  testId?: string;
}

/** What the panel can ask of the view directly, for things a prop cannot carry. */
export interface CodeEditorHandle {
  /** Put the caret on a 1-based line and scroll it into view (the minimap's click). */
  jumpToLine(line: number): void;
  /** The current scroll offset, so a silent reload can keep the reader's place. */
  scrollTop(): number;
  /**
   * Put the scroll offset back.
   *
   * A silent reload replaces the document in place, and a replaced document can leave the
   * reader at a different offset — so "keep my place" has to be a write, not just a hope
   * that nothing moved. Clamped by the browser to the scrollable height.
   */
  setScrollTop(top: number): void;
  /** Focus the editable surface, for a keyboard-first flow out of a dialog. */
  focus(): void;
}

/**
 * The colouring, as classes rather than inline styles, so `cockpit.css` owns the palette
 * and a theme change does not need a rebuild of the editor.
 */
const highlightStyle = HighlightStyle.define([
  { tag: tags.keyword, class: "tok-keyword" },
  { tag: [tags.string, tags.special(tags.string)], class: "tok-string" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], class: "tok-comment" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], class: "tok-number" },
  {
    tag: [tags.function(tags.variableName), tags.definition(tags.variableName)],
    class: "tok-function",
  },
  { tag: [tags.typeName, tags.className, tags.namespace], class: "tok-type" },
  { tag: [tags.propertyName, tags.attributeName], class: "tok-property" },
  { tag: [tags.operator, tags.compareOperator, tags.logicOperator], class: "tok-operator" },
  { tag: [tags.punctuation, tags.bracket], class: "tok-punct" },
  { tag: [tags.heading, tags.strong], class: "tok-heading" },
  { tag: [tags.link, tags.url], class: "tok-link" },
  { tag: tags.invalid, class: "tok-invalid" },
]);

/**
 * Phrases CodeMirror's own search panel prints, in Chinese.
 *
 * The panel is CodeMirror's, so its labels are not in this app's dictionary — it asks a
 * phrase table for each one. An English interface passes an empty table and gets
 * CodeMirror's English defaults; a Chinese one passes these. Handing Chinese to an English
 * interface would leave a host reading "查找" in a panel they opened from a "Find" menu.
 */
export const SEARCH_PHRASES_ZH: Record<string, string> = {
  Find: "查找",
  Replace: "替换",
  next: "下一个",
  previous: "上一个",
  all: "全部",
  "match case": "区分大小写",
  "by word": "全词匹配",
  regexp: "正则",
  replace: "替换",
  "replace all": "全部替换",
  close: "关闭",
  "current match": "当前命中",
  "on line": "所在行",
  "Go to line": "跳到行",
  go: "跳转",
  "replaced $ matches": "已替换 $ 处",
  "replaced match on line $": "已替换第 $ 行",
  "No results": "没有命中",
};

/** The phrase table for one interface language. */
function phrasesFor(uiLang: string | undefined): Record<string, string> {
  return uiLang === "en" ? {} : SEARCH_PHRASES_ZH;
}

/**
 * Counting stops here. A one-letter search in a large file would otherwise re-scan the
 * document on every keystroke the *editor* takes, not only on every keystroke the *panel*
 * takes, and the difference between 1000 and 12000 hits is not a difference anyone reads.
 */
const SEARCH_COUNT_CAP = 1000;

/**
 * `current / total` for the open find panel (item 6).
 *
 * CodeMirror's panel carries the query field, the case toggle and the two arrows, and stops
 * there: it announces a match to assistive technology but prints no number. "How many, and
 * which one am I on" is the one question a person looking for a hit cannot read off it, so
 * the count is walked here instead — `current` is the first match that ends at or after the
 * caret, which is the match the caret is on or the next one it can reach.
 */
export function searchMatchCountText(state: EditorState): string {
  const query = getSearchQuery(state);
  if (!query.valid || !query.search) return "";
  const caret = state.selection.main.head;
  let total = 0;
  let current = 0;
  let capped = false;
  for (const match of query.getCursor(state.doc)) {
    total += 1;
    if (!current && match.to >= caret) current = total;
    if (total >= SEARCH_COUNT_CAP) {
      capped = true;
      break;
    }
  }
  // The caret past the last match is on the last one, not on none.
  if (total && !current) current = total;
  return `${current} / ${total}${capped ? "+" : ""}`;
}

/**
 * The count, written into CodeMirror's own panel.
 *
 * The panel is CodeMirror's, so its DOM is where the number has to go: a separate box beside
 * it would be a second place to look. The panel is rebuilt every time it opens, so the span
 * is re-parented rather than assumed to have survived.
 */
const matchCount = ViewPlugin.fromClass(
  class {
    private readonly dom: HTMLElement;
    /** One retry per view update: without the bound, a closed panel reschedules for ever. */
    private retried = false;
    constructor(private readonly view: EditorView) {
      this.dom = document.createElement("span");
      this.dom.className = "cm-search-count";
      this.dom.setAttribute("data-testid", "editor-search-count");
      this.sync();
    }
    update(): void {
      this.retried = false;
      this.sync();
    }
    private sync(): void {
      const panel = this.view.dom.querySelector<HTMLElement>(".cm-panel.cm-search");
      if (!panel) {
        this.dom.remove();
        // On the turn that opens the panel, this plugin's update runs before the panel's DOM
        // is written, so a panel opened with a query already in hand would show no number
        // until the next keystroke. One microtask later it is there — once, not repeatedly.
        if (!this.retried) {
          this.retried = true;
          queueMicrotask(() => this.sync());
        }
        return;
      }
      if (this.dom.parentElement !== panel) {
        panel.insertBefore(this.dom, panel.querySelector('button[name="close"]') ?? null);
      }
      this.dom.textContent = searchMatchCountText(this.view.state);
    }
  },
);

export const CodeEditor = forwardRef<CodeEditorHandle, CodeEditorProps>(function CodeEditor(
  {
    text,
    language,
    readOnly,
    wrap,
    onCursor,
    onChange,
    onSave,
    initialScrollTop,
    onScrollTop,
    uiLang,
    testId,
  },
  ref,
) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // The extensions that can change while the view lives. A tab switch is a new document
  // (the panel keys on the path), but a toggle, a language flap or a locale change is not.
  const languageCompartment = useRef(new Compartment());
  const readOnlyCompartment = useRef(new Compartment());
  const wrapCompartment = useRef(new Compartment());
  const phrasesCompartment = useRef(new Compartment());
  // The callbacks as of this render. CodeMirror holds its extensions once, so a handler
  // captured at mount would call the first render's closure for ever.
  const onSaveRef = useRef(onSave);
  const onChangeRef = useRef(onChange);
  const onCursorRef = useRef(onCursor);
  const onScrollTopRef = useRef(onScrollTop);
  // True while this view is the one changing the document, so the `text` prop coming back
  // as an echo does not replace the buffer under the caret.
  const emitting = useRef(false);

  onSaveRef.current = onSave;
  onChangeRef.current = onChange;
  onCursorRef.current = onCursor;
  onScrollTopRef.current = onScrollTop;

  useImperativeHandle(
    ref,
    () => ({
      jumpToLine(line) {
        const view = viewRef.current;
        if (!view) return;
        const target = Math.max(1, Math.min(view.state.doc.lines, Math.trunc(line)));
        const info = view.state.doc.line(target);
        view.dispatch({
          selection: EditorSelection.cursor(info.from),
          scrollIntoView: true,
        });
        view.focus();
      },
      scrollTop() {
        return viewRef.current?.scrollDOM.scrollTop ?? 0;
      },
      setScrollTop(top) {
        const scroll = viewRef.current?.scrollDOM;
        if (scroll) scroll.scrollTop = Math.max(0, top);
      },
      focus() {
        viewRef.current?.focus();
      },
    }),
    [],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const view = new EditorView({
      parent: host,
      state: EditorState.create({
        doc: text,
        extensions: [
          lineNumbers(),
          highlightActiveLineGutter(),
          highlightSpecialChars(),
          history(),
          drawSelection(),
          dropCursor(),
          EditorState.allowMultipleSelections.of(true),
          indentOnInput(),
          bracketMatching(),
          closeBrackets(),
          rectangularSelection(),
          crosshairCursor(),
          highlightActiveLine(),
          highlightSelectionMatches(),
          matchCount,
          syntaxHighlighting(highlightStyle),
          phrasesCompartment.current.of(EditorState.phrases.of(phrasesFor(uiLang))),
          languageCompartment.current.of([]),
          readOnlyCompartment.current.of(readOnlyExtensions(readOnly)),
          wrapCompartment.current.of(wrap ? EditorView.lineWrapping : []),
          // Alt-click adds a caret; the platform's own click still moves the single one.
          EditorView.domEventHandlers({
            mousedown(event, view) {
              if (!event.altKey) return false;
              const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
              if (pos === null) return false;
              event.preventDefault();
              view.dispatch({
                selection: EditorSelection.create(
                  [...view.state.selection.ranges, EditorSelection.cursor(pos)],
                  view.state.selection.ranges.length,
                ),
                scrollIntoView: true,
              });
              return true;
            },
            scroll() {
              onScrollTopRef.current?.(viewRef.current?.scrollDOM.scrollTop ?? 0);
              return false;
            },
          }),
          keymap.of([
            {
              key: "Mod-s",
              preventDefault: true,
              run: () => {
                onSaveRef.current?.();
                return true;
              },
            },
            { key: "Mod-d", preventDefault: true, run: selectNextOccurrence },
            { key: "Mod-g", preventDefault: true, run: gotoLine },
            {
              key: "Mod-Alt-f",
              preventDefault: true,
              run: (view) => {
                const opened = openSearchPanel(view);
                // The panel carries a replace field; putting the caret in it is what makes
                // this key different from `Mod-f`. A microtask, because the panel is built
                // during the dispatch that opened it.
                queueMicrotask(() => {
                  view.dom
                    .querySelector<HTMLInputElement>('input[name="replace"]')
                    ?.focus();
                });
                return opened;
              },
            },
            ...closeBracketsKeymap,
            ...defaultKeymap,
            ...searchKeymap,
            ...historyKeymap,
            indentWithTab,
          ]),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              emitting.current = true;
              onChangeRef.current?.(update.state.doc.toString());
              emitting.current = false;
            }
            if (update.selectionSet || update.docChanged) {
              const head = update.state.selection.main.head;
              onCursorRef.current?.(cursorPositionAt(update.state.doc, head));
            }
          }),
        ],
      }),
    });
    viewRef.current = view;
    if (initialScrollTop) view.scrollDOM.scrollTop = initialScrollTop;
    const head = view.state.selection.main.head;
    onCursorRef.current?.(cursorPositionAt(view.state.doc, head));
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Mounted once: every later change is pushed in below. The first render's props are
    // the initial state, and re-running this would throw the buffer and the undo history
    // away on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A different document arriving from outside — a reload from disk, a discard, or the
  // silent re-read after an external change.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || emitting.current) return;
    if (view.state.doc.toString() === text) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      selection: { anchor: Math.min(view.state.selection.main.head, text.length) },
    });
  }, [text]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: readOnlyCompartment.current.reconfigure(readOnlyExtensions(readOnly)),
    });
  }, [readOnly]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: wrapCompartment.current.reconfigure(wrap ? EditorView.lineWrapping : []),
    });
  }, [wrap]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: phrasesCompartment.current.reconfigure(
        EditorState.phrases.of(phrasesFor(uiLang)),
      ),
    });
  }, [uiLang]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    let cancelled = false;
    void (async () => {
      const support: Extension | null = await languageSupportFor(language);
      if (cancelled || !viewRef.current) return;
      viewRef.current.dispatch({
        effects: languageCompartment.current.reconfigure(support ?? []),
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [language.name]);

  return (
    <div
      className="code-editor"
      data-testid={testId ?? "code-editor"}
      data-language={language.name ?? "plain"}
      data-readonly={readOnly ? "true" : "false"}
      data-wrap={wrap ? "true" : "false"}
      ref={hostRef}
    />
  );
});

/** Read-only has two halves: the state refuses changes, the view refuses the caret. */
function readOnlyExtensions(readOnly: boolean): Extension {
  if (!readOnly) return [];
  return [EditorState.readOnly.of(true), EditorView.editable.of(false)];
}
