/**
 * resultPanel.ts — 结果面板纯逻辑（Batch 3，从 vanilla `js/result-panel.js` 迁移）。
 *
 * 与 Sidebar/FileTree 同模式：组件可测的纯逻辑与 React 组件分离。
 * 覆盖 vanilla 中：Tab 状态机（文件/产物/打开文件 Tab，per-session 去重、上限 8）、
 * 产物登记（write/edit 成功文件，per-session 去重、上限 100）、路径工具
 * （extractFilePath / isImagePath / isMarkdownPath / isHtmlPath / detectLang）、
 * 宽度/折叠持久化（localStorage，分离键）。
 *
 * 外部契约（保持与 vanilla 一致）：window.emrg 桥不变；Batch 5 接线。
 */

// ── 常量（镜像 vanilla） ──
export const MAX_OPEN_TABS = 8;   // 打开文件 Tab 上限（决策点 6）
export const MAX_ARTIFACTS = 100; // 产物登记上限（P3.2 per-session）
export const MAX_ARTIFACTS_RENDER = 50; // 产物 pane 渲染上限（P1 遗留）
export const DEFAULT_WIDTH = 280;
export const MIN_WIDTH = 240;
export const MAX_WIDTH_RATIO = 0.45;
export const LS_WIDTH = "emrg.resultPanel.panelWidth";
export const LS_COLLAPSED = "emrg.resultPanel.collapsed";

// ── 类型 ──
export type TabId = "files" | "artifacts" | `file:${string}`;

export interface FileTab {
  path: string;
  name: string;
  content?: string;
  binary?: boolean;
  image?: boolean;
  html?: boolean;
  readError?: boolean;
  loading?: boolean;
}

export interface ArtifactRec {
  path: string;
  name: string;
  tool_name: string;
  elapsed?: number;
}

export interface PanelTabState {
  tabs: FileTab[];
  active: TabId;
}

// ── Tab 状态机（纯函数，不可变更新） ──
export function tabIdFor(path: string): TabId {
  return `file:${path}`;
}

export function isFileTabId(id: string): id is `file:${string}` {
  return typeof id === "string" && id.startsWith("file:");
}

/** 打开文件 Tab：同路径去重（激活既有）/ 上限 8 淘汰最旧（镜像 vanilla openFileTab） */
export function openFileTab(state: PanelTabState, path: string): PanelTabState {
  const existing = state.tabs.find((t) => t.path === path);
  if (existing) {
    return { ...state, active: tabIdFor(path) };
  }
  const name = String(path).split(/[\\/]/).pop() || path;
  const tab: FileTab = { path, name };
  const tabs = [...state.tabs, tab];
  while (tabs.length > MAX_OPEN_TABS) tabs.shift();
  return { tabs, active: tabIdFor(path) };
}

/** 关闭文件 Tab：激活位回退到最后一个 Tab 或 "artifacts"（镜像 vanilla closeFileTab） */
export function closeFileTab(state: PanelTabState, path: string): PanelTabState {
  const idx = state.tabs.findIndex((t) => t.path === path);
  if (idx < 0) return state;
  const tabs = state.tabs.filter((t) => t.path !== path);
  let active = state.active;
  if (active === tabIdFor(path)) {
    active = tabs.length ? tabIdFor(tabs[tabs.length - 1].path) : "artifacts";
  }
  return { tabs, active };
}

// ── 产物登记（镜像 vanilla addToolResult / extractFilePath / cleanPath） ──
export function addArtifact(list: ArtifactRec[], rec: ArtifactRec): ArtifactRec[] {
  const arr = list.filter((r) => r.path !== rec.path); // 同路径去重（移顶）
  arr.unshift(rec);
  if (arr.length > MAX_ARTIFACTS) arr.pop();
  return arr;
}

/** 从工具输出提取生成的文件路径（P3.2 改进 R4-①：优先首个绝对路径段，去扩展名依赖） */
export function extractFilePath(toolName: string, content: string): string {
  if (!content) return "";
  if (toolName === "write" || toolName === "edit") {
    // write: "Created /abs/path (N characters)" / "Updated /abs/path (N chars)"
    // edit:  "Made 1 replacement in /abs/path" / "Made 3 replacements in /abs/path"
    const m = content.match(/\/[^\s()]+/);
    if (m) return cleanPath(m[0]);
    // 兜底：旧关键词格式
    const km = content.match(/(?:Wrote|wrote|Written|created|已写入|写入)[^\n:：]*[:：]\s*([^\s\n]+)/);
    if (km) return cleanPath(km[1]);
    return "";
  }
  if (toolName === "bash") {
    const m = content.match(/(?:Created|created|Generated|generated)[^\n:：]*[:：]\s*([^\s\n]+)/);
    if (m) return cleanPath(m[1]);
  }
  return "";
}

export function cleanPath(p: string): string {
  return String(p).replace(/[`"'，,。、;；]/g, "").trim();
}

// ── 路径工具（镜像 vanilla） ──
const IMAGE_EXT = /\.(png|jpe?g|gif|svg|webp|bmp|ico)$/i;

export function isImagePath(path: string): boolean {
  return IMAGE_EXT.test(String(path).split(/[?#]/)[0]);
}
export function isMarkdownPath(path: string): boolean {
  return /\.(md|markdown|mdown)$/i.test(String(path));
}
export function isHtmlPath(path: string): boolean {
  return /\.(html?)$/i.test(String(path).split(/[?#]/)[0]);
}

/** 按文件扩展名推断语言（对齐 highlight.custom.js 已注册语言，镜像 vanilla detectLang） */
export function detectLang(path: string): string {
  const ext = String(path).split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = {
    py: "python", js: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
    sh: "bash", bash: "bash", zsh: "bash", css: "css", html: "html", htm: "html",
    json: "json", yml: "yaml", yaml: "yaml", toml: "ini", md: "markdown",
    go: "go", rs: "rust", java: "java", c: "c", h: "c", cpp: "cpp", cc: "cpp",
    sql: "sql", xml: "xml", ini: "ini", dockerfile: "dockerfile", diff: "diff",
  };
  if (path.toLowerCase().includes("dockerfile")) return "dockerfile";
  return map[ext] || "";
}

// ── 宽度/折叠持久化（镜像 vanilla；localStorage 失败静默） ──
export function clampWidth(w: number, viewportWidth?: number): number {
  const vw = (viewportWidth || 1200) * MAX_WIDTH_RATIO;
  return Math.round(Math.min(Math.max(w, MIN_WIDTH), Math.max(MIN_WIDTH, vw)));
}

export function storedWidth(): number {
  try {
    const v = parseInt(localStorage.getItem(LS_WIDTH) || "", 10);
    if (!Number.isNaN(v)) return clampWidth(v);
  } catch { /* ignore */ }
  return DEFAULT_WIDTH;
}

export function storedCollapsed(): boolean {
  try {
    return localStorage.getItem(LS_COLLAPSED) === "1";
  } catch { /* ignore */ }
  return false;
}

export function persistWidth(w: number): void {
  try { localStorage.setItem(LS_WIDTH, String(w)); } catch { /* ignore */ }
}
export function persistCollapsed(c: boolean): void {
  try { localStorage.setItem(LS_COLLAPSED, c ? "1" : "0"); } catch { /* ignore */ }
}
