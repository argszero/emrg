import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import {
  Compartment,
  EditorSelection,
  EditorState,
  type Extension,
  type Range,
} from "@codemirror/state";
import {
  Decoration,
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
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  HighlightStyle,
  bracketMatching,
  codeFolding,
  foldGutter,
  foldKeymap,
  indentOnInput,
  indentUnit,
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
  normalizeNewlines,
  type CursorPosition,
  type IndentUnit,
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
  /**
   * Identity of the document in this surface — the file's path.
   *
   * The editor is remounted per tab (a `key` on this component), and a remount is what
   * used to throw the undo history away. With a key, this names the entry in `stateStore`
   * that the mount adopts and the unmount writes back, so the history, the selection and
   * the folds live as long as the tab does.
   */
  docKey?: string;
  /**
   * Where a document's `EditorState` is kept between mounts, owned by the caller.
   *
   * A `Map` rather than component state because the whole point is to outlive the
   * component: a tab switch unmounts this editor, and a cache that lived here would be
   * destroyed by the very event it exists to survive.
   */
  stateStore?: Map<string, EditorState>;
  /** The text size in pixels, per session. A reading the panel owns and this applies. */
  fontSize?: number;
  /** What the document indents with, so `Enter` indents the way the file already does. */
  indent?: IndentUnit;
  /** `Mod-=` / `Mod+-` / `Mod+0`. `0` means "back to the default size". */
  onZoom?: (direction: -1 | 0 | 1) => void;
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
  // `next()` rather than `for … of`: the cursor is an Iterator, not an Iterable.
  const cursor = query.getCursor(state.doc);
  for (let step = cursor.next(); !step.done; step = cursor.next()) {
    total += 1;
    if (!current && step.value.to >= caret) current = total;
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

/**
 * The extension slots that change while a view lives, at module scope.
 *
 * A `Compartment` is an identity carried inside an `EditorState`, not an object owned by a
 * view, so one instance can serve every view — and here it *has* to. A state adopted from
 * the caller's store was built by an earlier mount, and its compartments must be the same
 * keys this mount reconfigures; a per-instance compartment would leave a returned tab
 * holding the read-only flag, the wrap setting and the language of the mount that built it,
 * with every toggle appearing to do nothing.
 *
 * A tab switch is a new document (the panel keys on the path), but a toggle, a language
 * flap or a locale change is not — those are what these five slots exist for.
 */
const languageCompartment = new Compartment();
const readOnlyCompartment = new Compartment();
const wrapCompartment = new Compartment();
const phrasesCompartment = new Compartment();
const indentCompartment = new Compartment();

/** The indent unit and tab stop together: they are one decision about one document. */
function indentExtensions(indent: IndentUnit): Extension {
  return [
    EditorState.tabSize.of(indent.width),
    indentUnit.of(indent.kind === "tabs" ? "\t" : " ".repeat(indent.width)),
  ];
}

/**
 * Indentation guides, drawn as a background rather than as widgets.
 *
 * The alternative — a widget or a marker element per level — puts DOM inside the text and
 * then has to keep it in step with every re-measure. A guide is not content: it belongs to
 * the line, so it is a line decoration carrying one number and the stylesheet does the rest.
 *
 * The number is a **count of levels past the first**, and the stylesheet multiplies it by the
 * width of one level in `ch`. Two consequences fall out for free: nothing in JavaScript has
 * to measure a font, and the guides follow a zoom or a proportional face without being told.
 */
const indentGuides = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = guidesFor(view);
    }
    update(update: ViewUpdate): void {
      if (update.docChanged || update.viewportChanged || update.geometryChanged) {
        this.decorations = guidesFor(update.view);
      }
    }
  },
  { decorations: (plugin) => plugin.decorations },
);

/**
 * One line decoration per visible line that is indented past its first level.
 *
 * `levels - 1` and not `levels`: a line at the first level has no guide inside it, because
 * the space before it *is* the level. What a reader sees on a three-deep line is the two
 * boundaries it sits inside, which is the structure being read. A line whose indentation is
 * a mix of tabs and spaces is counted in columns — the tab stop is the same facet the editor
 * itself indents by, so the guides and `Enter` cannot disagree.
 */
function guidesFor(view: EditorView): DecorationSet {
  const unit = view.state.facet(indentUnit);
  const step = unit === "\t" ? view.state.tabSize : unit.length || 1;
  const ranges: Range<Decoration>[] = [];
  for (const { from, to } of view.visibleRanges) {
    for (let pos = from; pos <= to; ) {
      const line = view.state.doc.lineAt(pos);
      let columns = 0;
      for (const ch of line.text) {
        if (ch === " ") columns += 1;
        else if (ch === "\t") columns += step;
        else break;
      }
      const levels = Math.floor(columns / step);
      if (levels > 1) {
        ranges.push(
          Decoration.line({
            attributes: { style: `--indent-guides:${levels - 1}` },
          }).range(line.from),
        );
      }
      pos = line.to + 1;
    }
  }
  return Decoration.set(ranges, true);
}

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
    docKey,
    stateStore,
    fontSize,
    indent = { kind: "spaces", width: 2 },
    onZoom,
    testId,
  },
  ref,
) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);
  // The callbacks as of this render. CodeMirror holds its extensions once, so a handler
  // captured at mount would call the first render's closure for ever.
  const onSaveRef = useRef(onSave);
  const onChangeRef = useRef(onChange);
  const onCursorRef = useRef(onCursor);
  const onScrollTopRef = useRef(onScrollTop);
  const onZoomRef = useRef(onZoom);
  // True while this view is the one changing the document, so the `text` prop coming back
  // as an echo does not replace the buffer under the caret.
  const emitting = useRef(false);

  onSaveRef.current = onSave;
  onChangeRef.current = onChange;
  onCursorRef.current = onCursor;
  onScrollTopRef.current = onScrollTop;
  onZoomRef.current = onZoom;

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

    // A state this key already had: the undo history, the selection and the folds of the tab
    // this one replaced. Adopting it is the whole fix for a returned tab's dead `Cmd+Z` —
    // remounting used to build a fresh state, and a document replaced through a dispatch
    // records the *replacement* rather than restoring what the reader typed. The alignment
    // effects below then bring the five compartments up to this render's props, and mount is
    // one of the runs they make, so an adopted state is reconfigured rather than trusted.
    const adopted = docKey ? stateStore?.get(docKey) : undefined;
    const zoom = (direction: -1 | 0 | 1): boolean => {
      onZoomRef.current?.(direction);
      return true;
    };

    const view = new EditorView({
      parent: host,
      state:
        adopted ??
        EditorState.create({
          doc: text,
          extensions: [
            lineNumbers(),
            highlightActiveLineGutter(),
            highlightSpecialChars(),
            // Both halves of folding: the gutter draws the arrows, and `foldGutter` brings the
            // fold state field with it (measured: `state.field(foldState, false)` is present
            // with it alone). Without the field the effects are accepted and ignored, which is
            // the shape of a feature that looks wired and folds nothing.
            foldGutter(),
            codeFolding(),
            history(),
            drawSelection(),
            dropCursor(),
            EditorState.allowMultipleSelections.of(true),
            indentCompartment.of(indentExtensions(indent)),
            indentOnInput(),
            bracketMatching(),
            closeBrackets(),
            rectangularSelection(),
            crosshairCursor(),
            highlightActiveLine(),
            highlightSelectionMatches(),
            indentGuides,
            matchCount,
            syntaxHighlighting(highlightStyle),
            phrasesCompartment.of(EditorState.phrases.of(phrasesFor(uiLang))),
            languageCompartment.of([]),
            readOnlyCompartment.of(readOnlyExtensions(readOnly)),
            wrapCompartment.of(wrap ? EditorView.lineWrapping : []),
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
            // Folding before the defaults, because a keymap only ever gets a key the earlier
            // ones left alone: `foldKeymap` owns the bracket spellings and would be shadowed.
            ...foldKeymap,
            { key: "Mod-=", preventDefault: true, run: () => zoom(1) },
            // The same key with the shift most layouts add for `+`, which is what a reader
            // presses when they mean "bigger".
            { key: "Mod-Shift-=", preventDefault: true, run: () => zoom(1) },
            { key: "Mod--", preventDefault: true, run: () => zoom(-1) },
            { key: "Mod-_", preventDefault: true, run: () => zoom(-1) },
            { key: "Mod-0", preventDefault: true, run: () => zoom(0) },
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
      // Before `destroy`, because the state is the only thing that outlives this view and it
      // is the tab's: its history, its selection, its folds. A caller that passes no `docKey`
      // is a caller with one editor and nothing to keep, so the store is left alone.
      if (docKey && stateStore) stateStore.set(docKey, view.state);
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
    // Compared in one line ending, because the editor keeps one: CodeMirror splits a
    // document on `\r\n?|\n` and its own text is always `\n`. Comparing the raw strings made
    // a CRLF file look stale the moment it mounted, so the panel replaced the whole buffer
    // and reported the normalized text back up — which is how a Windows file quietly became
    // a Unix one without anybody typing.
    if (view.state.doc.toString() === normalizeNewlines(text)) return;
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      selection: { anchor: Math.min(view.state.selection.main.head, text.length) },
    });
  }, [text]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: indentCompartment.reconfigure(indentExtensions(indent)),
    });
    // The two numbers, not the object: a caller may build `{kind, width}` inline on every
    // render, and depending on its identity would reconfigure the view on every keystroke.
  }, [indent.kind, indent.width]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: readOnlyCompartment.reconfigure(readOnlyExtensions(readOnly)),
    });
  }, [readOnly]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: wrapCompartment.reconfigure(wrap ? EditorView.lineWrapping : []),
    });
  }, [wrap]);

  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    view.dispatch({
      effects: phrasesCompartment.reconfigure(
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
        effects: languageCompartment.reconfigure(support ?? []),
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
      // The two numbers CSS cannot know: the reader's chosen text size, and how wide one
      // indent level is. Both are custom properties rather than a style on `.cm-content`,
      // because CodeMirror rewrites its own children and this element is the one React owns.
      style={
        {
          "--editor-font-size": fontSize === undefined ? undefined : `${fontSize}px`,
          "--indent-unit-w": indent === undefined ? undefined : `${indent.width}ch`,
        } as React.CSSProperties
      }
      ref={hostRef}
    />
  );
});

/** Read-only has two halves: the state refuses changes, the view refuses the caret. */
function readOnlyExtensions(readOnly: boolean): Extension {
  if (!readOnly) return [];
  return [EditorState.readOnly.of(true), EditorView.editable.of(false)];
}
