import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useI18n } from "../lib/i18n";
import {
  EMPTY_EDITOR,
  NO_FILE_OPEN,
  activate,
  closeTab,
  openFile,
  pinTab,
  type EditorState,
} from "../lib/editor";
import {
  EditorPrefsStore,
  diskChange,
  languageFor,
  lineForRatio,
  minimapBars,
  readOnlyReason,
  type CursorPosition,
  type DiskChange,
  type DiskStamp,
  type ReadOnlyReason,
} from "../lib/editorDoc";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";

/**
 * EditorPanel — the cockpit's right-hand column, an editor rather than a tabbed reader
 * (issues #3068, #3095).
 *
 * The two file trees left this panel for the session column, so what is left here is one
 * surface with a job: edit the file a left-hand row was clicked on, and save it back. That
 * means a tab strip, a breadcrumb with the two display toggles, the editor itself, and a
 * status corner.
 *
 * The panel owns its own tabs and is driven imperatively (`openFile`), because the click
 * that opens a tab happens in a sibling column. The alternative — lifting the tab list
 * into the shell — would put the close-neighbour and preview-replacement rules in a
 * component that has nothing to do with them.
 *
 * Three pieces of per-file state, and the difference between them is the whole design:
 *
 * - **the snapshot** (`docs`) is what was last read from or written to disk, plus the
 *   `{mtime, size}` that snapshot is known to have had;
 * - **the buffer** (`buffers`) is what the editor currently holds;
 * - and **dirty** is exactly `buffer !== snapshot`. Deriving it rather than storing it
 *   means a save that fails cannot clear it by mistake: the snapshot moves only when a
 *   write actually returned.
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

/** Write one file; `null` = this build cannot write, and a refusal throws with its reason. */
export type SaveFile = (
  path: string,
  content: string,
  sid: string,
) => Promise<{ mtime: number | null; size: number | null } | null>;

/** Ask whether a file moved on disk; `null` = this build has no probe. */
export type StatFile = (
  path: string,
) => Promise<{ exists: boolean; mtime: number | null; size: number | null } | null>;

export interface EditorPanelHandle {
  /** Open a file in this panel: focus it, or open it as a preview / pinned tab. */
  openFile(req: { path: string; root: string; preview: boolean }): void;
}

export interface EditorPanelProps {
  sid?: string | null;
  loadFile?: LoadFile;
  saveFile?: SaveFile;
  statFile?: StatFile;
  /** Open the memory dialog; the door is the Memory tab's whole content. */

}

/** One file as the panel last knew it: the bytes, and the stamp those bytes had. */
interface Snapshot {
  content: FileContent | null;
  stamp: DiskStamp;
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

/** The refusal's own words when it has any, and an empty string when it does not. */
function messageOf(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  if (typeof error === "string" && error.trim()) return error.trim();
  return "";
}

export const EditorPanel = forwardRef<EditorPanelHandle, EditorPanelProps>(function EditorPanel(
  { sid = null, loadFile, saveFile, statFile },
  ref,
) {
  const { t, lang } = useI18n();
  const [state, setState] = useState<EditorState>(EMPTY_EDITOR);
  // What is on disk per path, and the stamp it is known to have. A present key holds the
  // answer — the file, or `null` for a read that failed — and an absent key means nothing
  // has come back yet.
  const [docs, setDocs] = useState<Record<string, Snapshot>>({});
  // What the editor holds per path. Set when a read answers, and changed only by typing.
  const [buffers, setBuffers] = useState<Record<string, string>>({});
  const [cursor, setCursor] = useState<CursorPosition>({ line: 1, column: 1 });
  // A save that failed, kept per path so it clears when that file saves or is reopened.
  const [saveFailed, setSaveFailed] = useState<{ path: string; message: string } | null>(null);
  // An external change that could not be applied silently, because the buffer had edits.
  const [conflict, setConflict] = useState<{
    path: string;
    kind: Exclude<DiskChange, null>;
    probed: DiskStamp;
  } | null>(null);
  // A close request held back by unsaved changes. The three answers are save / discard /
  // cancel, and cancel is the one that must leave the tab and its text exactly as they are.
  const [closeAsk, setCloseAsk] = useState<string | null>(null);
  // Which paths the panel has already asked about. A ref, not state, on purpose: as state
  // it would be a dependency of the load effect, so the effect would re-run the moment the
  // request was recorded, tear down the closure waiting on the answer, and the answer
  // would arrive into a dead scope — the panel then loads forever.
  const asked = useRef<Set<string>>(new Set());
  // Where each tab was scrolled to when it was last left, keyed by tab id, so two tabs on
  // different files keep their own place.
  const scrollByTab = useRef<Map<string, number>>(new Map());
  // The two display toggles, per session. The store is a ref so its identity is stable; the
  // counter is what makes a change to it render.
  const prefs = useRef(new EditorPrefsStore());
  const [prefsTick, setPrefsTick] = useState(0);
  const editorApi = useRef<CodeEditorHandle | null>(null);
  // The buffers and docs as of this render, for the async paths (a save, a probe) that
  // resolve after the render that started them and must not read a stale closure.
  const buffersRef = useRef(buffers);
  const docsRef = useRef(docs);
  buffersRef.current = buffers;
  docsRef.current = docs;

  useImperativeHandle(
    ref,
    () => ({
      openFile(req) {
        setState((prev) => openFile(prev, req));
      },
    }),
    [],
  );

  const activeFile = state.tabs.find((tab) => tab.id === state.active) ?? null;
  const display = useMemo(() => prefs.current.get(sid), [sid, prefsTick]);

  /** The text this path's editor shows: the buffer if it has one, else the snapshot. */
  const textFor = useCallback(
    (path: string): string => buffers[path] ?? docs[path]?.content?.content ?? "",
    [buffers, docs],
  );

  /**
   * Dirty is derived, never stored.
   *
   * A stored flag has one failure mode that matters and no way back from it: a save that
   * throws after the flag was cleared leaves the panel claiming a file is on disk when it
   * is not. Deriving it from `buffer !== snapshot` makes that impossible — the snapshot
   * moves only when a write returned.
   */
  const isDirty = useCallback(
    (path: string): boolean => {
      const snapshot = docs[path]?.content?.content;
      const buffer = buffers[path];
      return buffer !== undefined && snapshot !== undefined && buffer !== snapshot;
    },
    [buffers, docs],
  );

  const activeSnapshot = activeFile ? docs[activeFile.path] : undefined;
  const activeContent = activeSnapshot?.content ?? null;
  const activeText = activeFile ? textFor(activeFile.path) : "";
  const activeDirty = activeFile ? isDirty(activeFile.path) : false;
  const activeLanguage = useMemo(() => languageFor(activeFile?.path ?? ""), [activeFile?.path]);
  const readOnlyWhy: ReadOnlyReason | null = activeContent
    ? readOnlyReason({
        binary: activeContent.binary,
        truncated: activeContent.truncated,
        size: activeSnapshot?.stamp.size ?? null,
      })
    : null;
  const lineCount = useMemo(
    () => (activeText === "" ? 1 : activeText.split("\n").length),
    [activeText],
  );
  const bars = useMemo(
    () => (display.minimap && activeContent ? minimapBars(activeText) : []),
    [display.minimap, activeContent, activeText],
  );

  /**
   * Record what a read answered, as both snapshot and buffer.
   *
   * The stamp is probed separately: the reader answers content, and the two facts the
   * external-change check needs — mtime and size — are what a stat is for. A probe that
   * answers nothing leaves both `null`, which `diskChange` treats as "not measured" rather
   * than as a change.
   */
  const recordRead = useCallback(
    async (path: string, content: FileContent | null): Promise<void> => {
      let stamp: DiskStamp = { mtime: null, size: null };
      if (statFile) {
        try {
          const probed = await statFile(path);
          if (probed) stamp = { exists: probed.exists, mtime: probed.mtime, size: probed.size };
        } catch {
          /* an unmeasurable probe is not a change, and not a load failure either */
        }
      }
      setDocs((prev) => ({ ...prev, [path]: { content, stamp } }));
      if (content) setBuffers((prev) => ({ ...prev, [path]: content.content }));
    },
    [statFile],
  );

  // Read the active file once per path. The panel asks the daemon rather than reading
  // anything itself: the daemon owns the file system, exactly as it does for the trees.
  useEffect(() => {
    if (!activeFile) return;
    const path = activeFile.path;
    if (asked.current.has(path)) return;
    asked.current.add(path);
    // A build without a reader is not a pending read: answer it at once, so the panel shows
    // its "cannot read" note instead of a spinner that never ends.
    if (!loadFile) {
      setDocs((prev) => ({
        ...prev,
        [path]: { content: null, stamp: { mtime: null, size: null } },
      }));
      return;
    }
    let cancelled = false;
    void (async () => {
      let answer: FileContent | null = null;
      try {
        answer = await loadFile(path);
      } catch {
        answer = null;
      }
      if (cancelled) return;
      await recordRead(path, answer);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeFile, loadFile, recordRead]);

  /**
   * Save one path.
   *
   * The order is the point: the snapshot moves **after** the write returns, so a refusal —
   * the daemon judging the target outside the session's boundary, a disk that filled, a
   * file replaced by a directory — leaves the buffer dirty and the dot on the tab. A save
   * that reported success and then failed to move the snapshot is the one failure this
   * cannot have, because there is nothing else to move.
   */
  const save = useCallback(
    async (path: string): Promise<boolean> => {
      const text = buffersRef.current[path] ?? docsRef.current[path]?.content?.content ?? "";
      if (!saveFile || !sid) {
        setSaveFailed({ path, message: t("editor.saveUnavailable") });
        return false;
      }
      try {
        const written = await saveFile(path, text, sid);
        setDocs((prev) => {
          const current = prev[path];
          if (!current?.content) return prev;
          return {
            ...prev,
            [path]: {
              // A whole-file save makes the snapshot exactly the buffer, and a buffer that
              // now holds the whole file is no longer truncated.
              content: { ...current.content, content: text, truncated: false },
              // What the write itself reported. Without this the very next probe would
              // read the panel's own save as somebody else's change and prompt about it.
              stamp: { exists: true, mtime: written?.mtime ?? null, size: written?.size ?? null },
            },
          };
        });
        setSaveFailed((prev) => (prev?.path === path ? null : prev));
        return true;
      } catch (error) {
        const reason = messageOf(error);
        setSaveFailed({
          path,
          message: reason ? `${t("editor.saveFailed")}：${reason}` : t("editor.saveFailed"),
        });
        return false;
      }
    },
    [saveFile, sid, t],
  );

  /**
   * Ask the disk whether the file under an open tab moved, and act on the answer.
   *
   * Untouched buffer: reload silently and keep the reader's place — no question, because
   * there is nothing to lose. Edited buffer: ask, because both answers are legitimate and
   * only the host knows which one they meant.
   */
  const probe = useCallback(
    async (path: string): Promise<void> => {
      if (!statFile) return;
      let probed: DiskStamp | null = null;
      try {
        const answer = await statFile(path);
        probed = answer
          ? { exists: answer.exists, mtime: answer.mtime, size: answer.size }
          : null;
      } catch {
        return;
      }
      const change = diskChange(docsRef.current[path]?.stamp ?? null, probed);
      if (!change || !probed) return;
      const snapshot = docsRef.current[path]?.content?.content;
      const buffer = buffersRef.current[path];
      const edited = buffer !== undefined && snapshot !== undefined && buffer !== snapshot;
      if (edited) {
        setConflict({ path, kind: change, probed });
        return;
      }
      const top = editorApi.current?.scrollTop() ?? 0;
      let reread: FileContent | null = null;
      try {
        reread = loadFile ? await loadFile(path) : null;
      } catch {
        reread = null;
      }
      await recordRead(path, reread);
      // A reload replaces the document in place, and this puts the reader back where they
      // were: "未动过则静默重载并保持滚动位置" is the acceptance's own wording.
      scrollByTab.current.set(path, top);
      editorApi.current?.setScrollTop(top);
    },
    [statFile, loadFile, recordRead],
  );

  // Probe when a tab becomes active, and when the window regains focus — the two moments a
  // file can plausibly have changed underneath the panel without the host doing anything.
  useEffect(() => {
    const path = activeFile?.path;
    if (!path) return;
    void probe(path);
  }, [activeFile?.path, probe]);

  useEffect(() => {
    const onFocus = (): void => {
      const path = state.active === NO_FILE_OPEN ? null : state.active;
      if (path) void probe(path);
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [state.active, probe]);

  // Restore the tab's own scroll position when it becomes active again. Written in a
  // layout effect so the position is in place before the frame is painted: a restore in a
  // plain effect would show the new tab at the previous tab's offset for one frame.
  useLayoutEffect(() => {
    editorApi.current?.setScrollTop(scrollByTab.current.get(state.active) ?? 0);
  }, [state.active]);

  /** Reload one path from disk on the host's word, keeping their place. */
  const reloadFromDisk = useCallback(
    async (path: string): Promise<void> => {
      const top = editorApi.current?.scrollTop() ?? 0;
      let reread: FileContent | null = null;
      try {
        reread = loadFile ? await loadFile(path) : null;
      } catch {
        reread = null;
      }
      await recordRead(path, reread);
      scrollByTab.current.set(path, top);
      editorApi.current?.setScrollTop(top);
      setConflict((prev) => (prev?.path === path ? null : prev));
    },
    [loadFile, recordRead],
  );

  /**
   * Keep the buffer and stop asking.
   *
   * The recorded stamp moves to what the probe found, which is what makes this an answer
   * rather than a snooze: the file really is at that version as far as this panel is
   * concerned, and the next probe compares against it. The buffer keeps its edits, so the
   * tab stays dirty and a save will overwrite the version that was just declined.
   */
  const keepMine = useCallback((path: string, probed: DiskStamp): void => {
    setDocs((prev) => {
      const current = prev[path];
      if (!current) return prev;
      return { ...prev, [path]: { ...current, stamp: probed } };
    });
    setConflict((prev) => (prev?.path === path ? null : prev));
  }, []);

  const requestClose = useCallback(
    (tabId: string): void => {
      if (isDirty(tabId)) {
        setCloseAsk(tabId);
        return;
      }
      setState((prev) => closeTab(prev, tabId));
    },
    [isDirty],
  );

  const answerClose = useCallback(
    async (answer: "save" | "discard" | "cancel"): Promise<void> => {
      const path = closeAsk;
      setCloseAsk(null);
      if (!path || answer === "cancel") return;
      if (answer === "save") {
        const saved = await save(path);
        // A save that failed keeps the tab: closing it would throw the only copy away.
        if (!saved) return;
      }
      setState((prev) => closeTab(prev, path));
    },
    [closeAsk, save],
  );

  const toggle = useCallback(
    (patch: { wrap?: boolean; minimap?: boolean }): void => {
      prefs.current.set(sid, patch);
      setPrefsTick((n) => n + 1);
    },
    [sid],
  );

  const languageLabel = activeLanguage.name ?? t("editor.plainText");
  const readOnlyNote =
    readOnlyWhy === "large"
      ? t("editor.readOnlyLarge")
      : readOnlyWhy === "truncated"
        ? t("editor.readOnlyTruncated")
        : readOnlyWhy === "binary"
          ? t("editor.readOnlyBinary")
          : null;

  return (
    <section className="editor" data-testid="editor-panel" data-sid={sid ?? ""}>
      <div
        className="editor-tabs"
        role="tablist"
        data-testid="editor-tabs"
        // The strip scrolls inside the panel's own width and never wraps: a tab is never
        // squeezed, and the panel is never widened by how many tabs are open.
        style={{ overflowX: "auto", overflowY: "hidden", flexWrap: "nowrap", minWidth: 0, maxWidth: "100%" }}
        onWheel={(e) => {
          // A vertical wheel over a single-row strip has nothing to scroll vertically, so
          // it pushes the strip sideways instead.
          const el = e.currentTarget;
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) el.scrollLeft += e.deltaY;
        }}
      >
        {state.tabs.map((tab) => {
          const selected = tab.id === state.active;
          const dirty = isDirty(tab.id);
          return (
            <div
              key={tab.id}
              role="tab"
              aria-selected={selected}
              className={`editor-tab editor-tab-file${selected ? " active" : ""}`}
              data-testid="editor-tab"
              data-path={tab.path}
              data-preview={tab.preview ? "true" : "false"}
              data-dirty={dirty ? "true" : "false"}
              title={tab.path}
              style={{ flex: "0 0 auto" }}
              onClick={() => setState((prev) => activate(prev, tab.id))}
              // A double click pins, so the next single click on another file opens a second
              // tab instead of replacing this one.
              onDoubleClick={() => setState((prev) => pinTab(prev, tab.id))}
              onAuxClick={(e) => {
                if (e.button !== 1) return;
                e.preventDefault();
                requestClose(tab.id);
              }}
              // Middle click, the second spelling: browsers fire `auxclick` for a middle
              // button, but a synthetic press can arrive as a plain mousedown, and a close is
              // idempotent — the second call finds no such tab and changes nothing.
              onMouseDown={(e) => {
                if (e.button !== 1) return;
                e.preventDefault();
                requestClose(tab.id);
              }}
            >
              <span
                className="editor-tab-label"
                data-testid="editor-tab-label"
                style={tab.preview ? { fontStyle: "italic" } : undefined}
              >
                {baseName(tab.path)}
              </span>
              {/* The dot sits between the name and the close button, and is not a button: it
                  reports, and the close button is what acts on it. */}
              {dirty ? (
                <span
                  className="editor-tab-dirty"
                  data-testid="editor-tab-dirty"
                  title={t("editor.dirty")}
                  aria-label={t("editor.dirty")}
                >
                  ●
                </span>
              ) : null}
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
                  requestClose(tab.id);
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
          in after the code. The two toggles sit at its right end. */}
      {activeFile ? (
        <div className="editor-head" data-testid="editor-head">
          <nav className="editor-crumbs" data-testid="editor-breadcrumb">
            {crumbSegments(activeFile.path, activeFile.root).map((seg, i) => (
              <span key={`${seg}-${i}`}>
                {i > 0 ? <span className="editor-crumb-sep">/</span> : null}
                <span className="editor-crumb">{seg}</span>
              </span>
            ))}
          </nav>
          <div className="editor-toggles">
            <button
              type="button"
              className={`editor-toggle${display.wrap ? " on" : ""}`}
              data-testid="editor-toggle-wrap"
              aria-pressed={display.wrap}
              title={t("editor.wrap")}
              onClick={() => toggle({ wrap: !display.wrap })}
            >
              {t("editor.wrap")}
            </button>
            <button
              type="button"
              className={`editor-toggle${display.minimap ? " on" : ""}`}
              data-testid="editor-toggle-minimap"
              aria-pressed={display.minimap}
              title={t("editor.minimap")}
              onClick={() => toggle({ minimap: !display.minimap })}
            >
              {t("editor.minimap")}
            </button>
          </div>
        </div>
      ) : null}
      {closeAsk ? (
        <div className="editor-ask" data-testid="editor-close-ask">
          <span className="editor-ask-text">
            {t("editor.closeUnsaved", { name: baseName(closeAsk) })}
          </span>
          <button
            type="button"
            className="editor-ask-btn primary"
            data-testid="editor-close-save"
            onClick={() => void answerClose("save")}
          >
            {t("editor.save")}
          </button>
          <button
            type="button"
            className="editor-ask-btn"
            data-testid="editor-close-discard"
            onClick={() => void answerClose("discard")}
          >
            {t("editor.dontSave")}
          </button>
          <button
            type="button"
            className="editor-ask-btn"
            data-testid="editor-close-cancel"
            onClick={() => void answerClose("cancel")}
          >
            {t("editor.cancel")}
          </button>
        </div>
      ) : null}
      {activeFile && conflict ? (
        <div className="editor-ask" data-testid="editor-disk-conflict">
          <span className="editor-ask-text">
            {conflict.kind === "deleted" ? t("editor.deletedOnDisk") : t("editor.diskChanged")}
          </span>
          <button
            type="button"
            className="editor-ask-btn primary"
            data-testid="editor-conflict-reload"
            onClick={() => void reloadFromDisk(conflict.path)}
          >
            {t("editor.reload")}
          </button>
          <button
            type="button"
            className="editor-ask-btn"
            data-testid="editor-conflict-keep"
            onClick={() => keepMine(conflict.path, conflict.probed)}
          >
            {t("editor.keepMine")}
          </button>
        </div>
      ) : null}
      {activeFile && saveFailed && saveFailed.path === activeFile.path ? (
        <p className="editor-note editor-note-error" data-testid="editor-save-failed">
          {saveFailed.message}
        </p>
      ) : null}
      {activeFile && readOnlyNote ? (
        <p className="editor-note" data-testid="editor-readonly-note">
          {readOnlyNote}
        </p>
      ) : null}
      {activeFile && activeContent ? (
        <div className="editor-body" data-testid="editor-code">
          <CodeEditor
            ref={editorApi}
            text={activeText}
            language={activeLanguage}
            readOnly={readOnlyWhy !== null}
            wrap={display.wrap}
            onCursor={setCursor}
            onChange={(next) => setBuffers((prev) => ({ ...prev, [activeFile.path]: next }))}
            onSave={() => void save(activeFile.path)}
            // Written through on every scroll, so a silent reload can put the reader back.
            onScrollTop={(top) => scrollByTab.current.set(activeFile.path, top)}
            // A tab whose file was not read yet has no editor for one render, so switching
            // to it **mounts** this component fresh — and a mount is not a change of
            // `state.active`, so the restore effect above has already run by then. The
            // offset therefore has to arrive as a prop, or a returning tab opens at the top.
            initialScrollTop={scrollByTab.current.get(activeFile.path) ?? 0}
            uiLang={lang}
            testId="editor-surface"
          />
          {display.minimap ? (
            <div
              className="editor-minimap"
              data-testid="editor-minimap"
              onClick={(e) => {
                const box = e.currentTarget.getBoundingClientRect();
                if (box.height <= 0) return;
                const ratio = (e.clientY - box.top) / box.height;
                editorApi.current?.jumpToLine(lineForRatio(ratio, lineCount));
              }}
            >
              {bars.map((bar, i) => (
                <span
                  key={i}
                  className={`editor-minimap-bar${bar.filled ? " filled" : ""}`}
                  style={{ width: `${Math.max(4, Math.round(bar.width * 100))}%` }}
                />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
      {activeFile && activeSnapshot === undefined ? (
        <p className="editor-note" data-testid="editor-loading">
          {t("editor.loading")}
        </p>
      ) : null}
      {activeFile && activeSnapshot && activeSnapshot.content === null ? (
        <p className="editor-note editor-note-error" data-testid="editor-failed">
          {t("editor.failed")}
        </p>
      ) : null}
      {activeFile && activeContent ? (
        <div className="editor-status" data-testid="editor-status">
          <span className="editor-status-lang" data-testid="editor-status-language">
            {languageLabel}
          </span>
          <span className="editor-status-cursor" data-testid="editor-status-cursor">
            {t("editor.cursor", { line: cursor.line, column: cursor.column })}
          </span>
          {activeDirty ? (
            <span className="editor-status-dirty" data-testid="editor-status-dirty">
              {t("editor.dirty")}
            </span>
          ) : null}
        </div>
      ) : null}
    </section>
  );
});
