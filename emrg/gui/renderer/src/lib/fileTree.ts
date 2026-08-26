/**
 * fileTree.ts — Batch 3: 文件树纯逻辑（从 vanilla `js/file-tree.js` 提取）。
 *
 * 工作区文件浏览器（workspace panel P3.1，rant 2026-08-11T12:20:35；
 * VS Code 风格 UI 对齐，rant 2026-08-12T17:28:19）。
 * 本模块只含纯函数（图标映射 / 排序 / 根名解析）——加载状态机与展开态
 * 由 React 组件持有（useState/useEffect），类名与 vanilla CSS 一致（Batch 5 复用）。
 */

export interface FileEntry {
  name: string;
  path: string;
  type: "dir" | "file";
}

/** 文件行点击 → 打开查看器 Tab（vanilla: ResultPanel.openFileTab(sid, path)） */
export type OpenFileHandler = (path: string) => void;

/** 目录懒加载（vanilla: window.emrg.listFiles({ path }) → { entries }） */
export type ListFiles = (path: string) => Promise<{ entries: FileEntry[] }>;

// ── mono 内联 SVG 图标（16x16 viewBox，fill: currentColor 跟随文字色） ──
// （与 vanilla file-tree.js ICON 完全一致，仅提取路径 d）
export const ICONS = {
  chevronRight: '<path d="M6 3l5 5-5 5z"/>', // 折叠 ▸（目录可展开提示）
  chevronDown: '<path d="M3 6l5 5 5-5z"/>', // 展开 ▾
  dirClosed:
    '<path d="M1.5 2.5h4.1c.32 0 .62.13.84.36l1.1 1.14h6.96c.83 0 1.5.67 1.5 1.5V12.5c0 .83-.67 1.5-1.5 1.5H1.5A1.5 1.5 0 0 1 0 12.5V4c0-.83.67-1.5 1.5-1.5z"/>',
  dirOpen:
    '<path d="M1.5 2.5h4.1c.32 0 .62.13.84.36l1.1 1.14h6.96c.83 0 1.5.67 1.5 1.5V6H2.1c-.6 0-1.13.36-1.34.92L0 12.8V4c0-.83.67-1.5 1.5-1.5z"/><path d="M14.6 6.5H2.3c-.42 0-.8.27-.94.66L0 13.5h14.5c.83 0 1.5-.67 1.5-1.5V8c0-.83-.67-1.5-1.5-1.5z"/>',
  fileDefault:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  fileImg:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="6" cy="6.5" r="1.2"/><path d="M4.5 12.5l2.5-2.5 2 2 1.5-1.5 1.5 2z"/>',
  fileMd:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11h4.5"/>',
  fileJson:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7.2 6.5L5.5 8.5l1.7 2M8.8 6.5l1.7 2-1.7 2"/>',
  fileCode:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.5 6.5L4.5 8.5l2 2M9.5 6.5l2 2-2 2"/>',
  fileYml:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 6.5h.01M5 9h.01M5 11.5h.01M8 6.5h3M8 9h3M8 11.5h3"/>',
  fileTxt:
    '<path d="M3 1h6.5l3.5 3.5V15H3V1z"/><path d="M9.5 1v3.5H13" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 6h7M4.5 8.5h7M4.5 11h7"/>',
} as const;

/** 扩展名 → 图标 key（与 vanilla EXT_ICON 完全一致） */
const EXT_ICON: Record<string, string> = {
  png: "fileImg", jpg: "fileImg", jpeg: "fileImg", gif: "fileImg", svg: "fileImg",
  webp: "fileImg", bmp: "fileImg", ico: "fileImg",
  md: "fileMd", markdown: "fileMd",
  json: "fileJson",
  js: "fileCode", jsx: "fileCode", mjs: "fileCode", cjs: "fileCode",
  ts: "fileCode", tsx: "fileCode",
  html: "fileCode", htm: "fileCode", css: "fileCode", scss: "fileCode",
  py: "fileCode", rb: "fileCode", go: "fileCode", rs: "fileCode", java: "fileCode", c: "fileCode", cpp: "fileCode",
  yml: "fileYml", yaml: "fileYml", toml: "fileYml", cfg: "fileYml", ini: "fileYml",
  txt: "fileTxt", log: "fileTxt", csv: "fileTxt", tsv: "fileTxt",
};

type IconKey = keyof typeof ICONS;

function isIconKey(k: string): k is IconKey {
  return k in ICONS;
}

/** 条目图标：目录 → 开/合文件夹；文件 → 按扩展名映射（未知 → 通用文件） */
export function iconFor(entry: { type: string; name: string }, expanded: boolean): string {
  if (entry.type === "dir") return expanded ? ICONS.dirOpen : ICONS.dirClosed;
  const ext = String(entry.name).split(".").pop()?.toLowerCase() || "";
  const key = EXT_ICON[ext] || "fileDefault";
  return isIconKey(key) ? ICONS[key] : ICONS.fileDefault;
}

/** 目录行箭头图标（展开 ▾ / 折叠 ▸） */
export function chevronFor(expanded: boolean): string {
  return expanded ? ICONS.chevronDown : ICONS.chevronRight;
}

/** 根目录名：取路径末段（vanilla: split(/[\\/]/).filter(Boolean).pop() || path） */
export function rootNameFrom(path: string): string {
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.pop() || path;
}

/**
 * 条目排序：目录在前、按名排序（daemon 已排序，此处防御性兜底——
 * vanilla file-tree.js 依赖 daemon 序，React 版显式保证幂等）。
 * 返回新数组（不修改入参）。
 */
export function sortEntries(entries: FileEntry[]): FileEntry[] {
  return [...entries].sort((a, b) => {
    if (a.type !== b.type) return a.type === "dir" ? -1 : 1;
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });
}
