import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import {
  EMPTY_EDITOR,
  MEMORY_TAB_ID,
  activate,
  closeTab,
  openFile,
  pinTab,
  type EditorState,
} from "../lib/editor";

/**
 * EditorPanel — the cockpit's right-hand column, an editor rather than a tabbed reader
 * (issue #3068).
 *
 * The two file trees left this panel for the session column, so what is left here is one
 * surface with a job: show the file a left-hand row was clicked on. That means a tab
 * strip, a breadcrumb, and a read-only code area, plus one tab that is not a file at all
 * — Memory, which holds the door to the memory dialog and cannot be closed, so the panel
 * always has a subject.
 *
 * The panel owns its own tabs and is driven imperatively (`openFile`), because the click
 * that opens a tab happens in a sibling column. The alternative — lifting the tab list
 * into the shell — would put the close-neighbour and preview-replacement rules in a
 * component that has nothing to do with them.
 */

/** What the daemon answers for one file; mirrors the `read_file` tool's frame. */
export interface FileContent {
  content: string;
  binary: boolean;
  truncated: boolean;
  totalLines: number | null;
}

/** Read one file; `null` = this build cannot read files, or the read failed. */
export type LoadFile = (path: string) => Promise<FileContent | null>;

export interface EditorPanelHandle {
  /** Open a file in this panel: focus it, or open it as a preview / pinned tab. */
  openFile(req: { path: string; root: string; preview: boolean }): void;
}

export interface EditorPanelProps {
  sid?: string | null;
  loadFile?: LoadFile;
  /** Open the memory dialog; the door is the Memory tab's whole content. */
  onOpenMemory?: () => void;
}

/** The last path segment, for a tab's label. */
function baseName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}

/** The path cut into a root and the segments under it, for the breadcrumb. */
function crumbSegments(path: string, root: string): string[] {
  const trim = (p: string): string => p.replace(/[\\/]+$/, "");
  const base = trim(root);
  if (base && (path === base || path.startsWith(`${base}/`) || path.startsWith(`${base}\\`))) {
    const rest = path.slice(base.length).split(/[\\/]/).filter(Boolean);
    return [base, ...rest];
  }
  return path.split(/[\\/]/).filter(Boolean);
}

export const EditorPanel = forwardRef<EditorPanelHandle, EditorPanelProps>(function EditorPanel(
  { sid = null, loadFile, onOpenMemory },
  ref,
) {
  const { t } = useI18n();
  const [state, setState] = useState<EditorState>(EMPTY_EDITOR);
  // Contents by path. A key that is present holds the answer — the file, or `null` for a
  // read that failed — and an absent key means nothing has come back yet.
  const [contents, setContents] = useState<Record<string, FileContent | null>>({});
  // Which paths the panel has already asked about. A ref, not state, on purpose: as state
  // it would be a dependency of the effect below, so the effect would re-run the moment the
  // request was recorded, tear down the closure that is waiting on the answer, and the
  // answer would arrive into a dead scope — the panel then loads forever.
  const asked = useRef<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Where each tab was scrolled to when it was last left (issue #3068, item 10). Keyed by
  // tab id, so two tabs on different files keep their own place.
  const scrollByTab = useRef<Map<string, number>>(new Map());

  useImperativeHandle(
    ref,
    () => ({
      openFile(req) {
        setState((prev) => openFile(prev, req));
      },
    }),
    [],
  );

  const activeFile = state.active === MEMORY_TAB_ID ? null : state.tabs.find((tab) => tab.id === state.active) ?? null;

  // Load the active file's text once per path. The panel asks the daemon rather than
  // reading anything itself: the daemon owns the file system, exactly as it does for the
  // trees' directory listings.
  useEffect(() => {
    if (!activeFile) return;
    const path = activeFile.path;
    if (asked.current.has(path)) return;
    asked.current.add(path);
    // A build without a reader is not a pending read: answer it at once, so the panel shows
    // its "cannot read" note instead of a spinner that never ends.
    if (!loadFile) {
      setContents((prev) => ({ ...prev, [path]: null }));
      return;
    }
    void (async () => {
      let answer: FileContent | null = null;
      try {
        answer = await loadFile(path);
      } catch {
        answer = null;
      }
      setContents((prev) => ({ ...prev, [path]: answer }));
    })();
  }, [activeFile, loadFile]);

  // Restore the tab's own scroll position when it becomes active again. Written in a
  // layout effect so the position is in place before the frame is painted: a restore in
  // a plain effect would show the new tab at the previous tab's offset for one frame.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = scrollByTab.current.get(state.active) ?? 0;
  }, [state.active]);

  const onScroll = useCallback((): void => {
    const el = scrollRef.current;
    if (!el) return;
    scrollByTab.current.set(state.active, el.scrollTop);
  }, [state.active]);

  // A present key holds the answer — the file, or `null` for a read that failed — and only
  // an absent key means nothing has come back yet. The `??` that used to sit here folded
  // the stored `null` back into `undefined`, so the "cannot read" note below was
  // unreachable and a failed read spun for ever.
  const activeContent = activeFile
    ? Object.hasOwn(contents, activeFile.path)
      ? contents[activeFile.path]
      : undefined
    : undefined;
  const text = activeContent?.content ?? "";
  const lineCount = useMemo(() => (text === "" ? 1 : text.split("\n").length), [text]);
  // The gutter is as wide as the widest number it prints, so a 4-line and a 4000-line
  // file do not jump the text sideways between tabs.
  const gutterWidth = `${String(lineCount).length + 1}ch`;

  return (
    <section className="editor" data-testid="editor-panel" data-sid={sid ?? ""}>
      <div
        className="editor-tabs"
        role="tablist"
        data-testid="editor-tabs"
        // The strip scrolls inside the panel's own width and never wraps: a tab is never
        // squeezed, and the panel is never widened by how many tabs are open (item 12).
        style={{ overflowX: "auto", overflowY: "hidden", flexWrap: "nowrap", minWidth: 0, maxWidth: "100%" }}
        onWheel={(e) => {
          // A vertical wheel over a single-row strip has nothing to scroll vertically,
          // so it pushes the strip sideways instead.
          const el = e.currentTarget;
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) el.scrollLeft += e.deltaY;
        }}
      >
        <button
          type="button"
          role="tab"
          aria-selected={state.active === MEMORY_TAB_ID}
          className={`editor-tab${state.active === MEMORY_TAB_ID ? " active" : ""}`}
          data-testid="editor-tab-memory"
          style={{ flex: "0 0 auto" }}
          onClick={() => setState((prev) => activate(prev, MEMORY_TAB_ID))}
        >
          {t("inspector.tabMemory")}
        </button>
        {state.tabs.map((tab) => {
          const selected = tab.id === state.active;
          return (
            <div
              key={tab.id}
              role="tab"
              aria-selected={selected}
              className={`editor-tab editor-tab-file${selected ? " active" : ""}`}
              data-testid="editor-tab"
              data-path={tab.path}
              data-preview={tab.preview ? "true" : "false"}
              title={tab.path}
              style={{ flex: "0 0 auto" }}
              onClick={() => setState((prev) => activate(prev, tab.id))}
              // A double click pins, so the next single click on another file opens a
              // second tab instead of replacing this one.
              onDoubleClick={() => setState((prev) => pinTab(prev, tab.id))}
              onAuxClick={(e) => {
                if (e.button !== 1) return;
                e.preventDefault();
                setState((prev) => closeTab(prev, tab.id));
              }}
              // Middle click, the second spelling: browsers fire `auxclick` for a middle
              // button, but a synthetic press can arrive as a plain mousedown, and a
              // close is idempotent — the second call finds no such tab and changes
              // nothing.
              onMouseDown={(e) => {
                if (e.button !== 1) return;
                e.preventDefault();
                setState((prev) => closeTab(prev, tab.id));
              }}
            >
              <span
                className="editor-tab-label"
                data-testid="editor-tab-label"
                style={tab.preview ? { fontStyle: "italic" } : undefined}
              >
                {baseName(tab.path)}
              </span>
              <button
                type="button"
                className="editor-tab-close"
                data-testid="editor-tab-close"
                title={t("editor.close")}
                aria-label={t("editor.close")}
                onClick={(e) => {
                  // The close button sits inside the tab, so without this the click would
                  // also activate the tab it just closed.
                  e.stopPropagation();
                  setState((prev) => closeTab(prev, tab.id));
                }}
              >
                ✕
              </button>
            </div>
          );
        })}
      </div>
      {/* The breadcrumb follows the active tab, not the read: which file this is is known
          the moment the tab exists, and waiting for the text would make the header flicker
          in after the code. */}
      {activeFile ? (
        <nav className="editor-crumbs" data-testid="editor-breadcrumb">
          {crumbSegments(activeFile.path, activeFile.root).map((seg, i) => (
            <span key={`${seg}-${i}`}>
              {i > 0 ? <span className="editor-crumb-sep">/</span> : null}
              <span className="editor-crumb">{seg}</span>
            </span>
          ))}
        </nav>
      ) : null}
      {activeFile && activeContent ? (
        <>
          <div className="editor-scroll" data-testid="editor-code" ref={scrollRef} onScroll={onScroll}>
            <div className="editor-inner">
              {/* The gutter is a sticky first column of the scroll container, so the
                  line numbers stay put when the code scrolls sideways (item 11). */}
              <div
                className="editor-gutter"
                data-testid="editor-gutter"
                style={{ position: "sticky", left: 0, width: gutterWidth }}
              >
                {Array.from({ length: lineCount }, (_, i) => (
                  <div key={i}>{i + 1}</div>
                ))}
              </div>
              <pre className="editor-text" data-testid="editor-text">
                {activeContent && activeContent.binary ? t("editor.binary") : text}
              </pre>
            </div>
          </div>
        </>
      ) : null}
      {activeFile && activeContent === undefined ? (
        <p className="editor-note" data-testid="editor-loading">
          {t("editor.loading")}
        </p>
      ) : null}
      {activeFile && activeContent === null ? (
        <p className="editor-note editor-note-error" data-testid="editor-failed">
          {t("editor.failed")}
        </p>
      ) : null}
      {activeFile && activeContent?.truncated ? (
        <p className="editor-note" data-testid="editor-truncated">
          {t("editor.truncated")}
        </p>
      ) : null}
      {state.active === MEMORY_TAB_ID ? (
        <div className="editor-memory" data-testid="editor-memory-door">
          <p className="editor-note">{t("inspector.memoryHint")}</p>
          <button
            type="button"
            className="inspector-door"
            data-testid="editor-memory-open"
            onClick={onOpenMemory}
          >
            {t("inspector.openMemory")}
          </button>
        </div>
      ) : null}
    </section>
  );
});
