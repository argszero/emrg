import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useI18n } from "../lib/i18n";
import {
  activate,
  activeTab,
  breadcrumb,
  closeTab,
  fileLines,
  pinTab,
  type EditorState,
} from "../lib/editor";

/**
 * EditorPanel — the page's right-hand section: one tab strip over one read-only code
 * area (issue #3068).
 *
 * It used to be three tabs holding one thing at a time, two of them the directory trees.
 * The trees have moved under the session list, where a directory is a thing to read
 * beside the list of what is running rather than instead of it, and what is left here is
 * what a file needs: a tab per file, a breadcrumb saying which root it came from, and
 * the bytes.
 *
 * The first tab is the memory door and it is not a file: it has no ✕, it is never
 * replaced, and closing every file returns to it. Everything a tab *does* — preview
 * replaced by the next click, double click pins, ✕ or middle click closes, closing the
 * active tab moves right — is `lib/editor.ts`, so the rules are asserted without a DOM
 * and this component only draws them.
 *
 * The panel never claims a width. The strip scrolls inside whatever width the column is
 * given (`overflow-x` on the strip, no shrinking on the tabs), so a hundred open tabs
 * cannot widen the panel or give the page a horizontal scrollbar.
 */

/** What `emrg:readFile` answers, as main.js passes it on. */
export interface FileContent {
  content: string;
  binary: boolean;
  truncated: boolean;
  totalLines?: number;
}

export interface ReadFilePayload {
  path: string;
  startLine?: number;
  lineLimit?: number;
}

/**
 * Reads a file. `null` is "this build has no channel for it" — the bridge's own answer
 * when a packaged client predates the message — and it reads as its own sentence rather
 * than as an empty file, which is what a `""` would mean.
 */
export type ReadFile = (p: ReadFilePayload) => Promise<FileContent | null>;

/** One file's content in this panel's cache: read once, and the reading stays. */
type Entry =
  | { status: "loading" }
  | { status: "ok"; content: string; binary: boolean; truncated: boolean; totalLines?: number }
  | { status: "error"; message: string };

export interface EditorPanelProps {
  state: EditorState;
  onChange: (next: EditorState) => void;
  /** Reads a file; without it the code area says which build cannot. */
  readFile?: ReadFile;
  /** The root a tab's file sits under, for the breadcrumb. */
  rootOf?: (path: string) => string | null;
  /** Opens the memory dialog. The list itself belongs to that dialog, not here. */
  onOpenMemory?: () => void;
}

export function EditorPanel({ state, onChange, readFile, rootOf, onOpenMemory }: EditorPanelProps) {
  const { t } = useI18n();
  const [entries, setEntries] = useState<Record<string, Entry>>({});
  // Which paths have been asked for. A ref rather than a piece of state, because the
  // effect below writes the cache it would otherwise depend on — depending on `entries`
  // re-runs the effect on its own write, which is an endless loop rather than a read.
  const requested = useRef<Set<string>>(new Set());
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Every tab's reading position, by tab id. It lives here rather than in the code area
  // because switching tabs unmounts that area, and a position kept there would be lost
  // exactly when it is needed.
  const memory = useRef<Map<string, number>>(new Map());
  const restoredFor = useRef<string | null>(null);
  const active = activeTab(state);
  const activePath = active.kind === "file" ? active.path : null;

  // Read the file the active tab names, once. A failure is that tab's answer, not an
  // empty file: the difference is what the host needs to see.
  useEffect(() => {
    if (!activePath) return;
    if (requested.current.has(activePath)) return;
    requested.current.add(activePath);
    if (!readFile) {
      setEntries((prev) => ({ ...prev, [activePath]: { status: "error", message: t("editor.unavailable") } }));
      return;
    }
    let cancelled = false;
    setEntries((prev) => ({ ...prev, [activePath]: { status: "loading" } }));
    readFile({ path: activePath })
      .then((file) => {
        if (cancelled) return;
        if (file === null) {
          setEntries((prev) => ({ ...prev, [activePath]: { status: "error", message: t("editor.unavailable") } }));
          return;
        }
        setEntries((prev) => ({
          ...prev,
          [activePath]: {
            status: "ok",
            content: file.content ?? "",
            binary: file.binary === true,
            truncated: file.truncated === true,
            totalLines: file.totalLines,
          },
        }));
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setEntries((prev) => ({ ...prev, [activePath]: { status: "error", message } }));
      });
    return () => {
      cancelled = true;
    };
  }, [activePath, readFile, t]);

  const entry = activePath ? entries[activePath] : undefined;
  const content = entry?.status === "ok" && !entry.binary ? entry.content : null;

  // Put the tab back where it was read from. Keyed on the content as well as the tab,
  // because the first restore happens before the bytes have arrived.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (restoredFor.current === state.activeId && content !== null) {
      // Same tab, later render: the position is the host's to move, not ours.
    }
    const want = memory.current.get(state.activeId) ?? 0;
    el.scrollTop = want;
    restoredFor.current = state.activeId;
  }, [state.activeId, content]);

  const rememberScroll = useCallback((): void => {
    const el = scrollRef.current;
    if (!el) return;
    memory.current.set(state.activeId, el.scrollTop);
  }, [state.activeId]);

  const crumb = activePath ? breadcrumb(activePath, rootOf ? rootOf(activePath) : null) : null;
  const lines = content === null ? [] : fileLines(content);

  return (
    <section className="editor" data-testid="editor-panel" data-sid="">
      <div
        className="editor-strip"
        role="tablist"
        data-testid="editor-strip"
        data-tab-count={state.tabs.length}
        // The strip scrolls itself; nothing here may widen the panel.
        data-scrolls="x"
      >
        {state.tabs.map((tab) => (
          <div
            key={tab.id}
            role="tab"
            aria-selected={tab.id === state.activeId}
            className={`editor-tab${tab.id === state.activeId ? " active" : ""}${tab.pinned ? "" : " preview"}`}
            data-testid="editor-tab"
            data-tab-id={tab.id}
            data-kind={tab.kind}
            data-preview={tab.pinned ? "0" : "1"}
            data-active={tab.id === state.activeId ? "1" : "0"}
            data-shrink="0"
            title={tab.path ?? ""}
            onClick={() => onChange(activate(state, tab.id))}
            onDoubleClick={() => onChange(pinTab(state, tab.id))}
            onMouseDown={(e) => {
              // A middle click is a close, not the browser's autoscroll cursor.
              if (e.button === 1) e.preventDefault();
            }}
            onAuxClick={(e) => {
              if (e.button === 1) {
                e.preventDefault();
                onChange(closeTab(state, tab.id));
              }
            }}
          >
            <span className="editor-tab-name" data-testid="editor-tab-name">
              {tab.kind === "memory" ? t("inspector.tabMemory") : tab.title}
            </span>
            {tab.kind === "file" ? (
              <button
                type="button"
                className="editor-tab-close"
                data-testid="editor-tab-close"
                title={t("editor.closeTab")}
                onClick={(e) => {
                  e.stopPropagation();
                  onChange(closeTab(state, tab.id));
                }}
              >
                ✕
              </button>
            ) : null}
          </div>
        ))}
      </div>
      <div className="editor-body" data-testid="editor-body">
        {active.kind === "memory" ? (
          <div className="editor-memory" data-testid="editor-memory">
            <p className="editor-note">{t("inspector.memoryHint")}</p>
            <button
              type="button"
              className="editor-door"
              data-testid="editor-memory-open"
              onClick={onOpenMemory}
            >
              {t("inspector.openMemory")}
            </button>
          </div>
        ) : (
          <>
            <div className="editor-crumb" data-testid="editor-breadcrumb">
              {crumb?.root ? (
                <span className="editor-crumb-root" data-testid="editor-breadcrumb-root">
                  {crumb.root}
                </span>
              ) : null}
              {crumb?.parts.map((part, i) => (
                <span className="editor-crumb-part" key={`${part}-${i}`}>
                  {crumb.root || i > 0 ? "/" : ""}
                  {part}
                </span>
              ))}
            </div>
            {entry?.status === "error" ? (
              <p className="editor-note editor-note-error" data-testid="editor-error">
                {entry.message}
              </p>
            ) : entry?.status === "ok" && entry.binary ? (
              <p className="editor-note" data-testid="editor-binary">
                {t("editor.binary")}
              </p>
            ) : entry === undefined || entry.status === "loading" ? (
              <p className="editor-note" data-testid="editor-loading">
                {t("inspector.treeLoading")}
              </p>
            ) : (
              <>
                {entry.status === "ok" && entry.truncated ? (
                  <p className="editor-note" data-testid="editor-truncated">
                    {t("editor.truncated")}
                  </p>
                ) : null}
                <div className="editor-code" data-testid="editor-code" ref={scrollRef} onScroll={rememberScroll}>
                  <div className="editor-gutter" data-testid="editor-gutter" aria-hidden="true">
                    {lines.map((_, i) => (
                      <div className="editor-gutter-line" key={i}>
                        {i + 1}
                      </div>
                    ))}
                  </div>
                  <div className="editor-text" data-testid="editor-text">
                    {lines.map((line, i) => (
                      <div className="editor-line" data-testid="editor-line" key={i}>
                        {line === "" ? "\u200b" : line}
                      </div>
                    ))}
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
