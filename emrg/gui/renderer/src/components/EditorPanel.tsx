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
  closeOthers,
  closeTab,
  closeToTheRight,
  moveTab,
  openFile,
  pinTab,
  type EditorState,
} from "../lib/editor";
import {
  DEFAULT_FONT_SIZE,
  EditorPrefsStore,
  LARGE_FILE_BYTES,
  MAX_FONT_SIZE,
  MIN_FONT_SIZE,
  PAGE_LINES,
  applyLineEnding,
  diskChange,
  formatBytes,
  indentUnitFor,
  isHtmlPath,
  languageFor,
  lineEndingFor,
  lineForRatio,
  minimapLines,
  readFailureKey,
  readOnlyReason,
  type CursorPosition,
  type DiskChange,
  type DiskStamp,
  type ReadOnlyReason,
} from "../lib/editorDoc";
import { CodeEditor, type CodeEditorHandle } from "./CodeEditor";
import { ContextMenu } from "./ContextMenu";
import type { EditorState as CodeMirrorState } from "@codemirror/state";

/**
 * The toolbar's glyphs, drawn rather than written (rant 2026-11-27T17:55:15).
 *
 * Six words — Save, Reload, Discard, Wrap, Minimap, Browser — filled the row the path needs,
 * and the path is the only thing up there a reader cannot guess. Each control is now a 24×24
 * stroke drawing in the shape the rail and the status band already use, with its word moved
 * into `aria-label` and `title`: read out when someone asks for it, no width when nobody
 * does. Every path below is drawn for its own meaning rather than reused from another one.
 */
const TOOL_PATHS = {
  save: ["M5 4h11l3 3v13H5z", "M8 4v5h7V4", "M8 14h8v6H8z"],
  reload: ["M20 12a8 8 0 1 1-2.4-5.7", "M20 4v4h-4"],
  wrap: ["M3 6h18", "M3 12h12a3 3 0 0 1 0 6H9", "M11 15l-3 3 3 3"],
  minimap: ["M4 5h16v14H4z", "M15 5v14", "M7 9h5M7 12h5M7 16h3"],
  browser: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z", "M3 12h18", "M12 3a14 14 0 0 1 0 18", "M12 3a14 14 0 0 0 0 18"],
} as const;

const toolIcon = (paths: readonly string[]) => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    {paths.map((d) => (
      <path key={d} d={d} />
    ))}
  </svg>
);

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
  /** The file's size on disk, as the daemon measured it. `null` = not reported. */
  size: number | null;
}

/** One window of a file, as the daemon reads it. Both absent = the whole file. */
export interface ReadWindow {
  startLine?: number;
  lineLimit?: number;
}

/** Read one file, or one page of it; `null` = this build cannot read files, or the read failed. */
export type LoadFile = (path: string, window?: ReadWindow) => Promise<FileContent | null>;

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
  /** Why the read failed, when it did: the daemon's `error_code`, or `null`. */
  reason?: string | null;
  /** The 1-based line this snapshot starts at. `1` for a whole-file read. */
  startLine: number;
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

/**
 * What browser mode needs from the preload bridge, and nothing else.
 *
 * Every member is optional because the panel is also rendered where the bridge does not
 * exist — a jsdom test, a build without the main process. Browser mode then simply reports
 * nowhere, rather than throwing on the click that turns it on.
 */
interface BrowserBridge {
  showBrowser?: (payload: { path: string }) => Promise<{ ok: boolean; error?: string } | null>;
  hideBrowser?: () => Promise<unknown>;
  browserNavigate?: (
    payload: { target: string },
  ) => Promise<{ ok: boolean; error?: string } | null>;
  browserReload?: (payload: { target?: string }) => Promise<{ ok: boolean; error?: string } | null>;
  browserBounds?: (payload: {
    x: number;
    y: number;
    width: number;
    height: number;
  }) => Promise<unknown>;
}

function browserBridge(): BrowserBridge | undefined {
  return (window as unknown as { emrg?: BrowserBridge }).emrg;
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
  // One question with the shape of the close one, for the single act the toolbar offers:
  // the copy on disk replaces the buffer.
  const [reloadAsk, setReloadAsk] = useState<string | null>(null);
  // The open tab menu: which tab it was opened on, and where the pointer was.
  const [tabMenu, setTabMenu] = useState<{
    id: string;
    path: string;
    x: number;
    y: number;
  } | null>(null);
  const tabMenuRef = useRef<HTMLDivElement | null>(null);
  // Browser mode: the one path whose tab shows the page rather than its source. One path and
  // not a set, because the main process owns a single view — two tabs cannot both be the
  // page. `null` therefore means every tab is showing source.
  const [browserPath, setBrowserPath] = useState<string | null>(null);
  // The address bar's text, and why its last attempt failed — `empty`, `scheme` or `invalid`
  // in the policy module's own vocabulary.
  const [address, setAddress] = useState("");
  const [browserError, setBrowserError] = useState<string | null>(null);
  // The cell the native view is placed over. The view is painted above the DOM, so the only
  // thing that knows where the cell is, is the element the layout put there.
  const browserCell = useRef<HTMLDivElement | null>(null);
  // The tab a drag started on, so the drop knows what it is moving.
  const dragFrom = useRef<string | null>(null);
  // Each path's CodeMirror state, kept across the remount a tab switch causes. Here rather
  // than in the editor because the editor is what unmounts: this map is the thing that has
  // to outlive the very event it exists for.
  const editorStates = useRef<Map<string, CodeMirrorState>>(new Map());
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
  // Whether this tab is showing its page, and whether it could: only a page has a second way
  // to be read, so only a page gets a mode.
  const browserOn = activeFile !== null && browserPath === activeFile.path;
  const canBrowse = activeFile !== null && isHtmlPath(activeFile.path);
  const readOnlyWhy: ReadOnlyReason | null = activeContent
    ? readOnlyReason({
        binary: activeContent.binary,
        truncated: activeContent.truncated,
        // The reader's own size first: it comes with the content it belongs to, while the
        // stamp is a separate probe a build may not have at all.
        size: activeContent.size ?? activeSnapshot?.stamp.size ?? null,
        startLine: activeSnapshot?.startLine ?? 1,
      })
    : null;
  const lineCount = useMemo(
    () => (activeText === "" ? 1 : activeText.split("\n").length),
    [activeText],
  );
  const bars = useMemo(
    () => (display.minimap && activeContent ? minimapLines(activeText) : []),
    [display.minimap, activeContent, activeText],
  );
  // What the document itself says about its indentation and its line endings. Read from the
  // buffer rather than the snapshot: these are readings of the text the reader is looking at,
  // and the text they are about to save is the buffer.
  const indent = useMemo(() => indentUnitFor(activeText), [activeText]);
  const eol = useMemo(() => lineEndingFor(activeText), [activeText]);

  /**
   * Record what a read answered, as both snapshot and buffer.
   *
   * The stamp is probed separately: the reader answers content, and the two facts the
   * external-change check needs — mtime and size — are what a stat is for. A probe that
   * answers nothing leaves both `null`, which `diskChange` treats as "not measured" rather
   * than as a change.
   */
  const recordRead = useCallback(
    async (
      path: string,
      content: FileContent | null,
      reason: string | null = null,
      startLine = 1,
    ): Promise<void> => {
      let stamp: DiskStamp = { mtime: null, size: null };
      if (statFile) {
        try {
          const probed = await statFile(path);
          if (probed) stamp = { exists: probed.exists, mtime: probed.mtime, size: probed.size };
        } catch {
          /* an unmeasurable probe is not a change, and not a load failure either */
        }
      }
      setDocs((prev) => ({ ...prev, [path]: { content, stamp, reason, startLine } }));
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
        [path]: { content: null, stamp: { mtime: null, size: null }, startLine: 1 },
      }));
      return;
    }
    let cancelled = false;
    void (async () => {
      let answer: FileContent | null = null;
      let reason: string | null = null;
      try {
        answer = await loadFile(path);
      } catch (error) {
        // The reason is the whole point. This catch used to drop it into `null`, so a
        // file too large, a path that does not exist, a directory and a FIFO all drew the
        // same sentence (issue #3210).
        reason = (error as { errorCode?: string | null } | null)?.errorCode ?? null;
        answer = null;
      }
      if (cancelled) return;
      await recordRead(path, answer, reason, 1);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeFile, loadFile, recordRead]);

  /** How many lines one path's buffer holds — the span a page covers. */
  const linesIn = useCallback((path: string): number => {
    const text = buffersRef.current[path] ?? docsRef.current[path]?.content?.content ?? "";
    return text === "" ? 1 : text.split("\n").length;
  }, []);

  /** The window to re-read a path at: its own page, or `undefined` when it holds the file. */
  const windowFor = useCallback((path: string): ReadWindow | undefined => {
    const from = docsRef.current[path]?.startLine ?? 1;
    return from > 1 ? { startLine: from, lineLimit: PAGE_LINES } : undefined;
  }, []);

  /**
   * Read the page after the one on screen.
   *
   * A window the panel cannot advance hides the file rather than degrading it: the daemon
   * answers one page per request, so without this the first 2000 lines of a 100,000-line log
   * would be all a host ever saw. The page starts where the buffer ends and `startLine`
   * remembers that, which is also what keeps the buffer read only on the last page — there
   * `truncated` is correctly false, and an editable buffer would save 1,000 lines over a
   * 100,000-line file.
   */
  const nextPage = useCallback(async (): Promise<void> => {
    const path = activeFile?.path;
    if (!path || !loadFile) return;
    const from = (docsRef.current[path]?.startLine ?? 1) + linesIn(path);
    try {
      const page = await loadFile(path, { startLine: from, lineLimit: PAGE_LINES });
      await recordRead(path, page, null, from);
    } catch (error) {
      const reason = (error as { errorCode?: string | null } | null)?.errorCode ?? null;
      await recordRead(path, null, reason, from);
    }
  }, [activeFile?.path, loadFile, recordRead, linesIn]);

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
              // A save is only reachable while this is 1: a page that starts anywhere else
              // is read only (editorDoc.readOnlyReason), so nothing here can write a
              // fragment back over the file. Keeping the field keeps the snapshot whole.
              startLine: current.startLine,
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
      const from = docsRef.current[path]?.startLine ?? 1;
      try {
        reread = loadFile ? await loadFile(path, windowFor(path)) : null;
      } catch {
        reread = null;
      }
      await recordRead(path, reread, null, from);
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
      const from = docsRef.current[path]?.startLine ?? 1;
      try {
        reread = loadFile ? await loadFile(path, windowFor(path)) : null;
      } catch {
        reread = null;
      }
      await recordRead(path, reread, null, from);
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

  /**
   * The toolbar's one act on what the buffer holds: the copy on disk replaces it.
   *
   * A question only when there is something to lose. An untouched buffer is reloaded straight
   * away — asking "reload and lose your edits?" over a file with no edits is a dialog that
   * trains the reader to click through the one that matters.
   *
   * "Reload" and "discard my changes" were two buttons for this one act: both called
   * `reloadFromDisk`, and the only difference was which sentence asked. So there is one
   * entry and one sentence, the one that says what happens to the buffer rather than which
   * of two words the reader clicked (rant 2026-11-27T17:55:15).
   */
  const requestReload = useCallback(
    (path: string): void => {
      if (isDirty(path)) {
        setReloadAsk(path);
        return;
      }
      void reloadFromDisk(path);
    },
    [isDirty, reloadFromDisk],
  );

  const zoom = useCallback(
    (direction: -1 | 0 | 1): void => {
      const current = prefs.current.get(sid).fontSize;
      const next =
        direction === 0
          ? DEFAULT_FONT_SIZE
          : Math.min(MAX_FONT_SIZE, Math.max(MIN_FONT_SIZE, current + direction));
      prefs.current.set(sid, { fontSize: next });
      setPrefsTick((n) => n + 1);
    },
    [sid],
  );

  const closeMany = useCallback((id: string, how: "others" | "right"): void => {
    setState((prev) => (how === "others" ? closeOthers(prev, id) : closeToTheRight(prev, id)));
    setTabMenu(null);
  }, []);

  /**
   * Copy a tab's path.
   *
   * The clipboard is the window's, not this panel's, and a build can have none — so the
   * absence is a no-op rather than a second failure surface: the menu's job is the copy, and
   * there is nothing here that could report a missing clipboard usefully.
   */
  const copyTabPath = useCallback((path: string): void => {
    setTabMenu(null);
    void navigator.clipboard?.writeText(path).catch(() => {});
  }, []);

  // A menu anchored at a pointer has to go when the surface under it moves or the reader
  // presses Escape. The menu's own buttons stop their mousedown, so a pick is not an outside
  // click — which is why this listener is a plain one rather than a capture.
  useEffect(() => {
    if (!tabMenu) return;
    const close = (): void => setTabMenu(null);
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === "Escape") setTabMenu(null);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("resize", close);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("keydown", onKey);
    };
  }, [tabMenu]);

  // ── browser mode (issue #3214) ────────────────────────────────────────────────
  //
  // Turning it on shows this tab's page; leaving — another tab, the panel unmounting — hides
  // it. Hidden and not destroyed: the main process keeps the view, so coming back finds the
  // page where it was rather than reloaded.
  useEffect(() => {
    const api = browserBridge();
    if (!browserOn || !activeFile) {
      void api?.hideBrowser?.();
      return;
    }
    const pending = api?.showBrowser?.({ path: activeFile.path });
    if (pending) {
      void pending.then((answer) => {
        setBrowserError(answer?.ok === false ? answer.error ?? "invalid" : null);
      });
    }
    return () => {
      // A native view is painted above the DOM, so one left showing would float over
      // whatever replaced this panel.
      void api?.hideBrowser?.();
    };
  }, [browserOn, activeFile?.path]);

  // The cell, reported as the renderer measured it. The main process cannot compute this:
  // the view is painted above the DOM, so only the layout that placed the cell knows its box.
  useLayoutEffect(() => {
    if (!browserOn) return;
    const cell = browserCell.current;
    const api = browserBridge();
    if (!cell || !api?.browserBounds) return;
    const report = (): void => {
      const box = cell.getBoundingClientRect();
      void api.browserBounds?.({ x: box.left, y: box.top, width: box.width, height: box.height });
    };
    report();
    // Three things move the cell without moving this component: the column's width, the
    // column collapsing, and the window. The observer catches the first two, the listener
    // the third — and a zero box is the collapse, which the main process reads as "hide".
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(report) : null;
    observer?.observe(cell);
    window.addEventListener("resize", report);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", report);
    };
  }, [browserOn, activeFile?.path]);

  /** The address bar's Enter: the text goes through the policy module via the bridge, and a
   *  refusal comes back as the reason the bar shows under itself. */
  const goToAddress = useCallback(async (): Promise<void> => {
    const pending = browserBridge()?.browserNavigate?.({ target: address });
    if (!pending) return;
    const answer = await pending;
    setBrowserError(answer?.ok === false ? answer.error ?? "invalid" : null);
  }, [address]);

  /** The bar's reload: re-read the address the view is showing (issue #3229). No argument —
   *  what is on screen is the main process's to know, and asking it to re-read the text in the
   *  bar would reload whatever was typed but never opened. */
  const reloadBrowserPage = useCallback(async (): Promise<void> => {
    const pending = browserBridge()?.browserReload?.({});
    if (!pending) return;
    const answer = await pending;
    setBrowserError(answer?.ok === false ? answer.error ?? "invalid" : null);
  }, []);

  const toggle = useCallback(
    (patch: { wrap?: boolean; minimap?: boolean }): void => {
      prefs.current.set(sid, patch);
      setPrefsTick((n) => n + 1);
    },
    [sid],
  );

  const languageLabel = activeLanguage.name ?? t("editor.plainText");
  // A window is a fact with numbers in it, and the numbers are what make the notice a
  // fact rather than an apology: how large the file is, how many lines it has, how many
  // are on screen. The sentence it replaces said only that the file was long.
  const pageFrom = activeSnapshot?.startLine ?? 1;
  const pageTo = pageFrom + lineCount - 1;
  const total = activeContent?.totalLines ?? null;
  // Whether a page follows this one: `truncated` is the daemon saying there is more, and the
  // total is what turns that into the range the button offers.
  const canPage = activeContent?.truncated === true && total !== null && pageTo < total;
  const readOnlyNote = (() => {
    if (readOnlyWhy === "binary") return t("editor.readOnlyBinary");
    if (readOnlyWhy === "truncated") {
      const size = activeContent?.size ?? activeSnapshot?.stamp.size ?? null;
      if (total !== null && total > lineCount) {
        const range = { from: String(pageFrom), to: String(pageTo), total: String(total) };
        return size !== null && size > LARGE_FILE_BYTES
          ? t("editor.largePage", { size: formatBytes(size), ...range })
          : t("editor.showingFirst", range);
      }
      return t("editor.readOnlyTruncated");
    }
    if (readOnlyWhy === "large") return t("editor.readOnlyLarge");
    return null;
  })();

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
              // A drag moves the tab; the drop names the tab it landed on and `moveTab`
              // decides which side of it that is.
              draggable
              onDragStart={() => {
                dragFrom.current = tab.id;
              }}
              onDragOver={(e) => {
                // Only a tab drag is one: a file dragged in from the tree must not reorder.
                if (!dragFrom.current || dragFrom.current === tab.id) return;
                e.preventDefault();
              }}
              onDrop={(e) => {
                e.preventDefault();
                const from = dragFrom.current;
                dragFrom.current = null;
                if (from) setState((prev) => moveTab(prev, from, tab.id));
              }}
              onContextMenu={(e) => {
                e.preventDefault();
                setTabMenu({ id: tab.id, path: tab.path, x: e.clientX, y: e.clientY });
              }}
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
      {tabMenu ? (
        <ContextMenu
          anchor={{ x: tabMenu.x, y: tabMenu.y }}
          testId="editor-tab-menu"
          menuRef={tabMenuRef}
          active={-1}
          onHover={() => {
            /* the tab menu is pointer and keyboard-shortcut only: no arrow-key highlight */
          }}
          items={[
            {
              id: "close" as const,
              label: t("editor.close"),
              testId: "editor-tab-menu-close",
            },
            {
              id: "closeOthers" as const,
              label: t("editor.closeOthers"),
              testId: "editor-tab-menu-close-others",
            },
            {
              id: "closeRight" as const,
              label: t("editor.closeRight"),
              testId: "editor-tab-menu-close-right",
            },
            {
              id: "copyPath" as const,
              label: t("editor.copyPath"),
              testId: "editor-tab-menu-copy-path",
            },
          ]}
          onPick={(id) => {
            const path = tabMenu.path;
            if (id === "close") {
              setTabMenu(null);
              requestClose(path);
            } else if (id === "closeOthers") closeMany(path, "others");
            else if (id === "closeRight") closeMany(path, "right");
            else copyTabPath(path);
          }}
        />
      ) : null}
      {/* The breadcrumb follows the active tab, not the read: which file this is is known
          the moment the tab exists, and waiting for the text would make the header flicker
          in after the code. The path takes whatever width is going, and the controls beside
          it are each one glyph wide, so what a deep path loses here is nothing. */}
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
          <div className="editor-actions" data-testid="editor-actions">
            {/* The two acts that touch the file on disk. A mouse-only reader can save and can
                pick up a file that changed underneath them; the keyboard paths are unchanged.
                Save is live only while there is something to save: writing back the bytes
                that are already there has no outcome but a failure, so the button waits
                until the buffer differs from the snapshot (rant 2026-11-27T17:55:15). */}
            <button
              type="button"
              className="editor-toggle"
              data-testid="editor-action-save"
              aria-label={t("editor.save")}
              title={t("editor.save")}
              disabled={!activeDirty}
              onClick={() => void save(activeFile.path)}
            >
              {toolIcon(TOOL_PATHS.save)}
            </button>
            <button
              type="button"
              className="editor-toggle"
              data-testid="editor-action-reload"
              aria-label={t("editor.reload")}
              title={t("editor.reload")}
              onClick={() => requestReload(activeFile.path)}
            >
              {toolIcon(TOOL_PATHS.reload)}
            </button>
          </div>
          <div className="editor-toggles">
            <button
              type="button"
              className={`editor-toggle${display.wrap ? " on" : ""}`}
              data-testid="editor-toggle-wrap"
              aria-pressed={display.wrap}
              aria-label={t("editor.wrap")}
              title={t("editor.wrap")}
              onClick={() => toggle({ wrap: !display.wrap })}
            >
              {toolIcon(TOOL_PATHS.wrap)}
            </button>
            <button
              type="button"
              className={`editor-toggle${display.minimap ? " on" : ""}`}
              data-testid="editor-toggle-minimap"
              aria-pressed={display.minimap}
              aria-label={t("editor.minimap")}
              title={t("editor.minimap")}
              onClick={() => toggle({ minimap: !display.minimap })}
            >
              {toolIcon(TOOL_PATHS.minimap)}
            </button>
            {/* The third toggle, and the only one that changes what the tab *is* rather than
                how the text is drawn — so it exists only where there is a second thing to
                show, which is a page. */}
            {canBrowse ? (
              <button
                type="button"
                className={`editor-toggle${browserOn ? " on" : ""}`}
                data-testid="editor-toggle-browser"
                aria-pressed={browserOn}
                aria-label={t("editor.browser")}
                title={t("editor.browser")}
                onClick={() => {
                  setBrowserError(null);
                  setBrowserPath((prev) => (prev === activeFile.path ? null : activeFile.path));
                }}
              >
                {toolIcon(TOOL_PATHS.browser)}
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      {/* The address bar: the one row browser mode adds. It sits above the page rather than
          replacing the breadcrumb, so the tab strip, the breadcrumb and the status corner are
          the same in both modes. */}
      {activeFile && browserOn ? (
        <div className="editor-browser-bar" data-testid="editor-browser-bar">
          <input
            className="editor-address"
            data-testid="editor-address"
            type="text"
            value={address}
            placeholder={t("editor.addressHint")}
            aria-label={t("editor.address")}
            spellCheck={false}
            autoComplete="off"
            onChange={(e) => setAddress(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              void goToAddress();
            }}
          />
          <button
            type="button"
            className="editor-toggle"
            data-testid="editor-go"
            onClick={() => void goToAddress()}
          >
            {t("editor.go")}
          </button>
          {/* Re-reading the page is the whole reason this mode exists beside an editor
              (issue #3229): without it, a file edited on disk is only visible after the GUI
              restarts. Same verb as the editor's own reload, because it is the same act. */}
          <button
            type="button"
            className="editor-toggle"
            data-testid="editor-browser-reload"
            title={t("editor.reload")}
            onClick={() => void reloadBrowserPage()}
          >
            {t("editor.reload")}
          </button>
        </div>
      ) : null}
      {activeFile && browserOn && browserError ? (
        <p className="editor-note editor-note-error" data-testid="editor-browser-error">
          {browserError === "empty" ? t("editor.addressEmpty") : t("editor.addressInvalid")}
        </p>
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
      {reloadAsk ? (
        <div className="editor-ask" data-testid="editor-reload-ask">
          <span className="editor-ask-text">
            {t("editor.reloadUnsaved", { name: baseName(reloadAsk) })}
          </span>
          <button
            type="button"
            className="editor-ask-btn primary"
            data-testid="editor-reload-confirm"
            onClick={() => {
              const path = reloadAsk;
              setReloadAsk(null);
              void reloadFromDisk(path);
            }}
          >
            {t("editor.reload")}
          </button>
          <button
            type="button"
            className="editor-ask-btn"
            data-testid="editor-reload-cancel"
            onClick={() => setReloadAsk(null)}
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
          {canPage ? (
            <button
              type="button"
              className="editor-page-next"
              data-testid="editor-next-page"
              onClick={() => void nextPage()}
            >
              {t("editor.nextPage", {
                from: String(pageTo + 1),
                to: String(Math.min(total ?? pageTo + PAGE_LINES, pageTo + PAGE_LINES)),
              })}
            </button>
          ) : null}
        </p>
      ) : null}
      {activeFile && (browserOn || activeContent) ? (
        <div className="editor-body" data-testid="editor-code">
          {/* Browser mode's cell. The page is a native view painted above the DOM, so this
              element draws nothing and takes no click: it exists to be measured, and the view
              is placed on exactly the box it reports. The source stays mounted underneath, so
              leaving the mode returns to the same undo history and scroll position. */}
          {browserOn ? (
            <div className="editor-browser" data-testid="editor-browser" ref={browserCell} />
          ) : null}
          <CodeEditor
            ref={editorApi}
            // One key per path. A tab switch therefore remounts the editor, and the mount
            // adopts this path's state back out of `editorStates` — which is what keeps the
            // undo history, the selection and the folds of a tab that was switched away from.
            key={activeFile.path}
            docKey={activeFile.path}
            stateStore={editorStates.current}
            text={activeText}
            language={activeLanguage}
            readOnly={readOnlyWhy !== null}
            wrap={display.wrap}
            indent={indent}
            fontSize={display.fontSize}
            onZoom={zoom}
            onCursor={setCursor}
            onChange={(next) =>
              setBuffers((prev) => ({
                ...prev,
                // The buffer keeps the ending its file has. The editor works in `\n`
                // internally, so without this a typed CRLF file comes back as LF and the
                // next save rewrites every line of it — the change the status corner's
                // line-ending reading exists to make visible before it happens.
                [activeFile.path]: applyLineEnding(
                  next,
                  lineEndingFor(docs[activeFile.path]?.content?.content ?? next),
                ),
              }))
            }
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
            <div className="editor-minimap" data-testid="editor-minimap">
              {/* The document's own lines, drawn small. A click on the drawn text jumps to
                  the line at that position, measured over the text rather than the column so
                  that a short file — whose map stops before the bottom — is not off by the
                  empty space below it. */}
              <div
                className="editor-minimap-code"
                data-testid="editor-minimap-code"
                onClick={(e) => {
                  const box = e.currentTarget.getBoundingClientRect();
                  if (box.height <= 0) return;
                  const ratio = (e.clientY - box.top) / box.height;
                  editorApi.current?.jumpToLine(lineForRatio(ratio, lineCount));
                }}
              >
                {bars.map((line, i) => (
                  <span key={i} className="editor-minimap-line">
                    {line}
                  </span>
                ))}
              </div>
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
          {t(readFailureKey(activeSnapshot.reason))}
        </p>
      ) : null}
      {activeFile && activeContent ? (
        <div className="editor-status" data-testid="editor-status">
          <span className="editor-status-lang" data-testid="editor-status-language">
            {languageLabel}
          </span>
          {/* Three readings the panel owes a file it is about to write back: what one level
              of indentation is, which line ending every line has, and the encoding the text
              came through. A whole-file save carries all three, so a change to any of them is
              a change to every line and has to be visible before it happens. */}
          <span className="editor-status-indent" data-testid="editor-status-indent">
            {indent.kind === "tabs"
              ? t("editor.indentTabs", { width: indent.width })
              : t("editor.indentSpaces", { width: indent.width })}
          </span>
          <span className="editor-status-eol" data-testid="editor-status-eol">
            {eol === "crlf" ? "CRLF" : eol === "cr" ? "CR" : "LF"}
          </span>
          <span className="editor-status-encoding" data-testid="editor-status-encoding">
            UTF-8
          </span>
          <span className="editor-status-font" data-testid="editor-status-font">
            {display.fontSize} px
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
