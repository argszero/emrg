import { useEffect, useState } from "react";
import { useI18n } from "../lib/i18n";
import { FileTree, type FileTreeProps } from "./FileTree";
import {
  addArtifact,
  closeFileTab,
  DEFAULT_WIDTH,
  detectLang,
  MIN_WIDTH,
  isFileTabId,
  isHtmlPath,
  isImagePath,
  isMarkdownPath,
  MAX_ARTIFACTS_RENDER,
  openFileTab,
  persistCollapsed,
  persistWidth,
  storedCollapsed,
  storedWidth,
  tabIdFor,
  type ArtifactRec,
  type FileTab,
  type PanelTabState,
  type TabId,
} from "../lib/resultPanel";

/**
 * ResultPanel — Batch 3: 结果面板（右栏：文件 / 产物 / 打开文件查看器 / HTML 预览）。
 * 从 vanilla `js/result-panel.js` 迁移为 React 组件。
 *
 * - Tab 状态机 / 产物登记 / 路径工具 / 宽度折叠持久化 → lib/resultPanel.ts（纯逻辑，可测）
 * - 文件 Tab「文件」内容 = <FileTree>（Batch 3 已迁移组件）
 * - 数据注入：listFiles/onOpenFile/readFile/openFile/t（不直接碰 daemon/IPC，Batch 5 接线）
 * - 类名与 vanilla CSS 一致（.result-panel/.result-tabbar/.result-filetab/.artifact-row/
 *   .viewer-*），Batch 5 CSS 直接复用
 */
export interface ReadFileResult {
  content?: string;
  binary?: boolean;
  totalLines?: number;
}
export type ReadFile = (req: { path: string }) => Promise<ReadFileResult>;
export type OpenFile = (req: { filePath: string }) => Promise<unknown>;

export interface ResultPanelProps {
  /** 当前会话 sid（per-session Tab/产物隔离；null = 默认桶） */
  sid?: string | null;
  /** 文件树根路径（「文件」Tab） */
  workspaceRoot?: string;
  /** 初始产物（来自 tool_finished 事件，Batch 5 接线） */
  artifacts?: ArtifactRec[];
  listFiles?: FileTreeProps["listFiles"];
  onOpenFile?: FileTreeProps["onOpenFile"];
  readFile?: ReadFile;
  openFile?: OpenFile;
  /** 测试可注入 i18n 文案 */
  t?: (key: string) => string;
  /**
   * 这一栏能长到多宽（issue #2959 item 4）。由 Shell 按窗口算出：窗口减去图标栏、
   * 左栏、两条 6px 拖拽带与正文下限。缺省时退回本组件的旧算法，因为它们不同——
   * 旧算法是窗口的 45%，那是「窗口的一半」，不是「正文还剩多少」。
   */
  maxWidth?: number;
}

const defaultT = (key: string): string => {
  const map: Record<string, string> = {
    "result.tabFiles": "Files",
    "result.tabArtifacts": "Artifacts",
    "result.empty": "No artifacts yet — files generated in this conversation will appear here",
    "result.viewerLoading": "Loading…",
    "result.viewerError": "Failed to load file",
    "result.viewerBinary": "Binary file",
    "result.viewerOpen": "Open",
    "result.htmlPreview": "HTML preview (embedded browser)",
  };
  return map[key] ?? key;
};

const noopRead: ReadFile = async () => ({ content: "" });
const noopOpen: OpenFile = async () => {};

export function ResultPanel({
  sid = null,
  workspaceRoot = "",
  artifacts = [],
  listFiles,
  onOpenFile,
  readFile = noopRead,
  openFile = noopOpen,
  t = defaultT,
  maxWidth,
}: ResultPanelProps) {
  const { t: ctxT } = useI18n();
  const tr = (k: string) => t(k) === k && ctxT(k) !== k ? ctxT(k) : t(k);
  const [tabState, setTabState] = useState<PanelTabState>({ tabs: [], active: "artifacts" });
  const [artifactList, setArtifactList] = useState<ArtifactRec[]>(artifacts);
  const [width, setWidth] = useState<number>(() => storedWidth());
  // `maxWidth` 的旧算法只在没人算出边界时使用（独立使用本组件的场景）
  const ceilingOf = (fallbackWindow: number) =>
    Math.max(MIN_WIDTH, maxWidth ?? Math.max(MIN_WIDTH, fallbackWindow * 0.45));
  const [collapsed, setCollapsed] = useState<boolean>(() => storedCollapsed());

  // 产物外部注入变化 → 合并登记（tool_finished 事件流）
  useEffect(() => {
    if (artifacts.length) setArtifactList((prev) => {
      let next = prev;
      for (const rec of artifacts) next = addArtifact(next, rec);
      return next;
    });
  }, [artifacts]);

  const activeTab = tabState.active;

  function activate(id: TabId) {
    setTabState((prev) => ({ ...prev, active: id }));
  }

  function onOpenTab(path: string) {
    setTabState((prev) => openFileTab(prev, path));
  }

  function onCloseTab(path: string) {
    setTabState((prev) => closeFileTab(prev, path));
  }

  function toggleCollapsed() {
    setCollapsed((prev) => {
      persistCollapsed(!prev);
      return !prev;
    });
  }

  function onResizeStart(e: React.MouseEvent) {
    e.preventDefault();
    // 拖拽期间指针离开这条 6px 也要保持 col-resize，并且不选字（issue #2959 item 4）
    document.body.classList.add("col-resizing");
    const startX = e.clientX;
    const startWidth = width;
    // Track the dragged width in a local var so mouseup persists the final
    // value, not the render-time `width` the closure captured (review #1006:
    // pre-drag width was being saved on mouseup, snapping back on reload).
    let currentWidth = startWidth;
    const ceiling = ceilingOf(window.innerWidth || 1200);
    const onMove = (ev: MouseEvent) => {
      const w = startWidth - (ev.clientX - startX);
      const cw = Math.min(Math.max(MIN_WIDTH, w), ceiling);
      currentWidth = cw;
      setWidth(cw);
    };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.classList.remove("col-resizing");
      persistWidth(currentWidth);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  /** 双击恢复默认宽度（与左栏的拖拽带同一个手势） */
  function resetWidth() {
    setWidth(DEFAULT_WIDTH);
    persistWidth(DEFAULT_WIDTH);
  }

  const activeTabInfo = tabState.tabs.find((tb) => tb.path === (isFileTabId(activeTab) ? activeTab.slice(5) : ""));

  return (
    <div
      className={`result-panel${collapsed ? " collapsed" : ""}`}
      data-testid="result-panel"
      style={collapsed ? { width: "40px" } : { width: `${width}px` }}
    >
      <button
        className="result-toggle"
        data-testid="result-toggle"
        type="button"
        title={tr("result.collapse")}
        onClick={toggleCollapsed}
      >
        {collapsed ? "»" : "«"}
      </button>

      {!collapsed && (
        <>
          <div className="result-tabbar" data-testid="result-tabbar">
            <button
              className={`result-tab files-tab${activeTab === "files" ? " active" : ""}`}
              data-testid="result-tab-files"
              type="button"
              onClick={() => activate("files")}
            >
              {tr("result.tabFiles")}
            </button>
            <button
              className={`result-tab artifacts-tab${activeTab === "artifacts" ? " active" : ""}`}
              data-testid="result-tab-artifacts"
              type="button"
              onClick={() => activate("artifacts")}
            >
              {tr("result.tabArtifacts")}
            </button>
            {tabState.tabs.map((tb) => (
              <div
                key={tb.path}
                className={`result-filetab${activeTab === tabIdFor(tb.path) ? " active" : ""}`}
                data-path={tb.path}
                data-testid="result-filetab"
              >
                <span
                  className="filetab-label"
                  onClick={() => activate(tabIdFor(tb.path))}
                >
                  {tb.name}
                </span>
                <span
                  className="filetab-close"
                  data-testid="filetab-close"
                  onClick={() => onCloseTab(tb.path)}
                >
                  ×
                </span>
              </div>
            ))}
          </div>

          <div className="result-body" data-testid="result-body">
            {activeTab === "files" && (
              <div className="result-files" data-testid="result-files">
                {workspaceRoot ? (
                  <FileTree
                    root={workspaceRoot}
                    listFiles={listFiles}
                    onOpenFile={(path) => {
                      onOpenFile && onOpenFile(path);
                      onOpenTab(path);
                    }}
                    t={tr}
                  />
                ) : (
                  <div className="result-empty" data-testid="result-files-empty">{tr("result.filesEmpty")}</div>
                )}
              </div>
            )}

            {activeTab === "artifacts" && (
              <div className="result-list" data-testid="result-artifacts">
                {artifactList.length === 0 && (
                  <div className="result-empty" data-testid="result-empty">{tr("result.empty")}</div>
                )}
                {artifactList.slice(0, MAX_ARTIFACTS_RENDER).map((rec) => (
                  <div
                    key={rec.path}
                    className="artifact-row"
                    data-path={rec.path}
                    data-testid="artifact-row"
                    onClick={() => onOpenTab(rec.path)}
                  >
                    <span className="artifact-name">{rec.name}</span>
                    <span className="artifact-rel">{rec.path}</span>
                  </div>
                ))}
              </div>
            )}

            {isFileTabId(activeTab) && (
              <Viewer
                tab={activeTabInfo || { path: activeTab.slice(5), name: activeTab.slice(5) }}
                readFile={readFile}
                openFile={openFile}
                t={tr}
              />
            )}
          </div>
        </>
      )}

      <div
        className="result-resizer"
        data-testid="result-resizer"
        role="separator"
        aria-orientation="vertical"
        onMouseDown={onResizeStart}
        onDoubleClick={(e) => {
          e.preventDefault();
          resetWidth();
        }}
      />
    </div>
  );
}

// ── 查看器（文本高亮 / md 渲染 / 图片直显 / 二进制提示 / HTML 占位） ──
function Viewer({
  tab,
  readFile,
  openFile,
  t,
}: {
  tab: FileTab;
  readFile: ReadFile;
  openFile: OpenFile;
  t: (key: string) => string;
}) {
  const [viewTab, setViewTab] = useState<FileTab>(tab);
  useEffect(() => {
    setViewTab(tab);
    let cancelled = false;
    const path = tab.path;
    // 无缓存 → 加载
    if (tab.content === undefined && !tab.image && !tab.html && !tab.readError) {
      if (isHtmlPath(path)) { setViewTab((prev) => ({ ...prev, html: true })); return; }
      if (isImagePath(path)) { setViewTab((prev) => ({ ...prev, image: true })); return; }
      setViewTab((prev) => ({ ...prev, loading: true }));
      readFile({ path })
        .then((res) => {
          if (cancelled) return;
          setViewTab({
            path,
            name: tab.name,
            content: typeof res.content === "string" ? res.content : "",
            binary: !!res.binary,
            readError: false,
          });
        })
        .catch(() => {
          if (cancelled) return;
          setViewTab({ path, name: tab.name, content: "", readError: true });
        });
    }
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab.path, tab.content, tab.image, tab.html, tab.readError]);

  async function openInSystem() {
    try { await openFile({ filePath: viewTab.path }); } catch { /* ignore */ }
  }

  return (
    <div className="result-viewer" data-testid="result-viewer">
      <div className="viewer-head">
        <span className="viewer-path">{viewTab.path}</span>
        <button className="viewer-open" type="button" onClick={openInSystem}>
          {t("result.viewerOpen")}
        </button>
      </div>
      {viewTab.readError && <div className="result-empty">{t("result.viewerError")}</div>}
      {viewTab.html && (
        <div className="viewer-html">
          <div className="result-empty">{t("result.htmlPreview")}</div>
        </div>
      )}
      {viewTab.binary && <div className="result-empty">{t("result.viewerBinary")}</div>}
      {viewTab.image && (
        <img className="viewer-img" src={`file://${viewTab.path}`} alt={viewTab.name} />
      )}
      {viewTab.loading && <div className="result-empty">{t("result.viewerLoading")}</div>}
      {viewTab.content !== undefined && !viewTab.readError && !viewTab.binary && (
        isMarkdownPath(viewTab.path)
          ? <pre className="viewer-md-plain">{viewTab.content}</pre>
          : <PlainCode path={viewTab.path} content={viewTab.content} />
      )}
    </div>
  );
}

/** 纯文本 + hljs 高亮（Batch 5 接真实 hljs；当前 hljs 不可用即转义展示） */
function PlainCode({ path, content }: { path: string; content: string }) {
  const lang = detectLang(path);
  // eslint-disable-next-line no-undef
  const hljs = (typeof window !== "undefined" ? (window as unknown as { hljs?: { highlight: (code: string, opts: { language: string; ignoreIllegals: boolean }) => { value: string } } }).hljs : undefined);
  let html = content.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  try {
    if (hljs && lang && hljs.highlight) {
      html = hljs.highlight(content, { language: lang, ignoreIllegals: true }).value;
    }
  } catch { /* highlight failure → escaped text */ }
  return (
    <pre className="viewer-pre">
      <code className={`hljs language-${lang || "plaintext"}`} dangerouslySetInnerHTML={{ __html: html }} />
    </pre>
  );
}
