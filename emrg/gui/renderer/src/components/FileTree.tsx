import { useEffect, useRef, useState } from "react";
import { chevronFor, iconFor, rootNameFrom, sortEntries, type FileEntry, type ListFiles, type OpenFileHandler } from "../lib/fileTree";

/**
 * FileTree — Batch 3: 工作区文件浏览器（Tab「文件」内容）。
 * 从 vanilla `js/file-tree.js` 迁移为 React 组件：
 *
 * - 懒加载目录树：目录行点击 → listFiles(path) 拉子项（已加载目录缓存，折叠不重新拉取）
 * - 展开态持久：Map<path, bool>（VS Code 行为，重新 render 不丢）；根默认展开
 * - 选中态：文件行点击 → .active（单选）+ onOpenFile(path)
 * - 类名与 vanilla CSS 完全一致（.ft-row/.ft-dir/.ft-file/.ft-head/.ft-icon/
 *   .ft-chevron/.ft-name/.ft-kids/.ft-hint/.result-empty），Batch 5 CSS 直接复用
 * - listFiles/onOpenFile 注入（不直接碰 daemon/IPC，Batch 5 接线）
 */
export interface FileTreeProps {
  root: string;
  listFiles?: ListFiles;
  onOpenFile?: OpenFileHandler;
  /** 测试可注入 i18n 文案（默认英文占位，Batch 5 接 t()） */
  t?: (key: string) => string;
}

interface DirState {
  loaded: boolean;
  loading: boolean;
  error: boolean;
  entries: FileEntry[];
}

const defaultT = (key: string): string => {
  switch (key) {
    case "result.treeLoading":
      return "Loading…";
    case "result.treeLoadFailed":
      return "Failed to load";
    case "result.filesEmpty":
      return "No workspace";
    default:
      return key;
  }
};

/** 默认 listFiles（Batch 5 前不接线 IPC；组件测试注入假实现） */
const noopList: ListFiles = async () => ({ entries: [] });

function FileTreeNode({
  entry,
  depth,
  expanded,
  expandedMap,
  selectedPath,
  dirs,
  onToggleDir,
  onSelectFile,
  onOpenFile,
  t,
}: {
  entry: FileEntry;
  depth: number;
  expanded: boolean;
  expandedMap: Map<string, boolean>;
  selectedPath: string | null;
  dirs: Map<string, DirState>;
  onToggleDir: (entry: FileEntry) => void;
  onSelectFile: (path: string) => void;
  onOpenFile: OpenFileHandler;
  t: (key: string) => string;
}) {
  const isDir = entry.type === "dir";
  const st = dirs.get(entry.path);
  const kids = isDir && st ? st.entries : [];

  return (
    <div
      className={`ft-row ${isDir ? "ft-dir" : "ft-file"}${selectedPath === entry.path ? " active" : ""}`}
      data-path={entry.path}
      style={{ paddingLeft: `${8 + depth * 16}px` }}
      onClick={(e) => {
        e.stopPropagation();
        if (isDir) onToggleDir(entry);
        else {
          onSelectFile(entry.path);
          onOpenFile(entry.path);
        }
      }}
    >
      <div className="ft-head">
        {isDir && (
          <span
            className="ft-chevron"
            dangerouslySetInnerHTML={{ __html: chevronFor(expanded) }}
          />
        )}
        <span className="ft-icon" dangerouslySetInnerHTML={{ __html: iconFor(entry, expanded) }} />
        <span className="ft-name">{entry.name}</span>
      </div>
      {isDir && (
        <div className={`ft-kids${expanded ? "" : " hidden"}`}>
          {expanded && st && st.loaded && st.error && (
            <div className="ft-hint">{t("result.treeLoadFailed")}</div>
          )}
          {expanded && st && st.loading && !st.loaded && (
            <div className="ft-hint">{t("result.treeLoading")}</div>
          )}
          {expanded &&
            st &&
            st.loaded &&
            kids.map((child) => (
              <FileTreeNode
                key={child.path}
                entry={child}
                depth={depth + 1}
                expanded={expandedMap.get(child.path) || false}
                expandedMap={expandedMap}
                selectedPath={selectedPath}
                dirs={dirs}
                onToggleDir={onToggleDir}
                onSelectFile={onSelectFile}
                onOpenFile={onOpenFile}
                t={t}
              />
            ))}
        </div>
      )}
    </div>
  );
}

export function FileTree({ root, listFiles = noopList, onOpenFile = () => {}, t = defaultT }: FileTreeProps) {
  const [dirs, setDirs] = useState<Map<string, DirState>>(new Map());
  const [expanded, setExpanded] = useState<Map<string, boolean>>(new Map());
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const listRef = useRef(listFiles);
  listRef.current = listFiles;

  // 切会话 → 重置根 + 清缓存（vanilla setSession/setRoot）
  useEffect(() => {
    setDirs(new Map());
    setExpanded(new Map([[root, true]]));
    setSelectedPath(null);
  }, [root]);

  const expandDir = async (path: string) => {
    let st = dirs.get(path);
    if (!st) {
      st = { loaded: false, loading: false, error: false, entries: [] };
      setDirs((prev) => new Map(prev).set(path, st!));
    }
    if (st.loaded || st.loading) return;
    setDirs((prev) => new Map(prev).set(path, { ...st!, loading: true }));
    try {
      const res = await listRef.current(path);
      setDirs((prev) => new Map(prev).set(path, { loaded: true, loading: false, error: false, entries: res.entries || [] }));
    } catch {
      setDirs((prev) => new Map(prev).set(path, { loaded: true, loading: false, error: true, entries: [] }));
    }
  };

  // 根自动展开（fire-and-forget）
  useEffect(() => {
    if (root) void expandDir(root);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root]);

  const toggleDir = (entry: FileEntry) => {
    const wasExpanded = expanded.get(entry.path) || false;
    if (wasExpanded) {
      setExpanded((prev) => new Map(prev).set(entry.path, false));
    } else {
      setExpanded((prev) => new Map(prev).set(entry.path, true));
      void expandDir(entry.path);
    }
  };

  if (!root) {
    return <div className="result-empty" data-testid="filetree-empty">{t("result.filesEmpty")}</div>;
  }

  const st = dirs.get(root);
  const rootExpanded = expanded.get(root) !== false; // 根默认展开
  const rootKids = st && st.loaded ? sortEntries(st.entries) : [];

  return (
    <div className="ft-tree" data-testid="filetree" data-root={root}>
      <div
        className="ft-row ft-dir ft-root"
        data-path={root}
        style={{ paddingLeft: "8px" }}
        onClick={(e) => {
          e.stopPropagation();
          toggleDir({ name: rootNameFrom(root), path: root, type: "dir" });
        }}
      >
        <div className="ft-head">
          <span className="ft-chevron" dangerouslySetInnerHTML={{ __html: chevronFor(rootExpanded) }} />
          <span className="ft-icon" dangerouslySetInnerHTML={{ __html: iconFor({ name: rootNameFrom(root), type: "dir" }, rootExpanded) }} />
          <span className="ft-name">{rootNameFrom(root)}</span>
        </div>
        <div className={`ft-kids${rootExpanded ? "" : " hidden"}`}>
          {rootExpanded && st && st.loading && !st.loaded && <div className="ft-hint">{t("result.treeLoading")}</div>}
          {rootExpanded && st && st.loaded && st.error && <div className="ft-hint">{t("result.treeLoadFailed")}</div>}
          {rootExpanded &&
            rootKids.map((child) => (
              <FileTreeNode
                key={child.path}
                entry={child}
                depth={1}
                expanded={expanded.get(child.path) || false}
                expandedMap={expanded}
                selectedPath={selectedPath}
                dirs={dirs}
                onToggleDir={toggleDir}
                onSelectFile={(p) => setSelectedPath(p)}
                onOpenFile={onOpenFile}
                t={t}
              />
            ))}
        </div>
      </div>
    </div>
  );
}
