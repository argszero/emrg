/**
 * resultPanel.test.ts — 结果面板纯逻辑测试（Batch 3）。
 * 镜像 vanilla result-panel.js 的 P3.2 逻辑：Tab 状态机、产物登记去重/上限、
 * extractFilePath 多形态、路径工具、宽度/折叠持久化。
 */
import { describe, expect, it, vi, afterEach, beforeAll, afterAll } from "vitest";
import {
  addArtifact,
  clampWidth,
  closeFileTab,
  detectLang,
  extractFilePath,
  isHtmlPath,
  isImagePath,
  isMarkdownPath,
  MAX_ARTIFACTS,
  MAX_OPEN_TABS,
  openFileTab,
  persistCollapsed,
  persistWidth,
  storedCollapsed,
  storedWidth,
  tabIdFor,
  type PanelTabState,
} from "./resultPanel";

const empty: PanelTabState = { tabs: [], active: "artifacts" };

// Node 22+ 的实验性 localStorage 全局在未传 --localstorage-file 时不可用（jsdom 被遮蔽），
// 这里用内存实现替换，保证持久化相关测试可运行（与 i18n.test 的 try/catch 策略等价）。
let savedLS: unknown;
const memLS = (() => {
  let store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
  };
})();
beforeAll(() => {
  savedLS = (globalThis as Record<string, unknown>).localStorage;
  (globalThis as Record<string, unknown>).localStorage = memLS;
});
afterAll(() => {
  (globalThis as Record<string, unknown>).localStorage = savedLS;
});

describe("resultPanel tab state machine", () => {
  it("openFileTab：新路径追加并激活；同路径去重激活既有", () => {
    const s1 = openFileTab(empty, "/a.py");
    expect(s1.tabs).toHaveLength(1);
    expect(s1.active).toBe("file:/a.py");
    const s2 = openFileTab(s1, "/b.py");
    expect(s2.tabs).toHaveLength(2);
    // 同路径 → 不追加
    const s3 = openFileTab(s2, "/a.py");
    expect(s3.tabs).toHaveLength(2);
    expect(s3.active).toBe("file:/a.py");
  });

  it("openFileTab：上限 8 淘汰最旧", () => {
    let s = empty;
    for (let i = 0; i < MAX_OPEN_TABS + 3; i++) {
      s = openFileTab(s, `/f${i}.py`);
    }
    expect(s.tabs).toHaveLength(MAX_OPEN_TABS);
    expect(s.tabs[0].path).toBe("/f3.py"); // f0/f1/f2 被淘汰
    expect(s.tabs[s.tabs.length - 1].path).toBe(`/f${MAX_OPEN_TABS + 2}.py`);
  });

  it("closeFileTab：关激活 Tab 回退到最后；关非激活不动 active", () => {
    let s = openFileTab(openFileTab(empty, "/a.py"), "/b.py");
    s = { ...s, active: tabIdFor("/a.py") };
    // 关非激活 b → active 保持 a
    const s1 = closeFileTab(s, "/b.py");
    expect(s1.active).toBe("file:/a.py");
    // 关激活 a → 回退到剩余最后（b 已关 → artifacts）
    const s2 = closeFileTab(s1, "/a.py");
    expect(s2.tabs).toHaveLength(0);
    expect(s2.active).toBe("artifacts");
  });
});

describe("resultPanel artifacts", () => {
  it("addArtifact：同路径去重移顶、保持相对顺序", () => {
    let arr = addArtifact([], { path: "/a.py", name: "a.py", tool_name: "write" });
    arr = addArtifact(arr, { path: "/b.py", name: "b.py", tool_name: "edit" });
    arr = addArtifact(arr, { path: "/a.py", name: "a.py", tool_name: "write" });
    expect(arr).toHaveLength(2);
    expect(arr[0].path).toBe("/a.py");
    expect(arr[1].path).toBe("/b.py");
  });

  it("addArtifact：上限 100", () => {
    let arr: { path: string; name: string; tool_name: string }[] = [];
    for (let i = 0; i < MAX_ARTIFACTS + 5; i++) {
      arr = addArtifact(arr, { path: `/f${i}.py`, name: `f${i}.py`, tool_name: "write" });
    }
    expect(arr).toHaveLength(MAX_ARTIFACTS);
  });
});

describe("extractFilePath", () => {
  it("write 绝对路径优先（含无扩展名文件）", () => {
    expect(extractFilePath("write", "Created /proj/Makefile (120 characters)")).toBe("/proj/Makefile");
    expect(extractFilePath("write", "Created /proj/src/app.py (42 characters)")).toBe("/proj/src/app.py");
    expect(extractFilePath("write", "Updated /proj/.env (10 chars)")).toBe("/proj/.env");
  });

  it("edit 多 replacement 形态", () => {
    expect(extractFilePath("edit", "Made 1 replacement in /proj/main.py")).toBe("/proj/main.py");
    expect(extractFilePath("edit", "Made 3 replacements in /proj/lib/util.py")).toBe("/proj/lib/util.py");
  });

  it("非 write/edit/bash 返回空", () => {
    expect(extractFilePath("read", "Read /etc/passwd")).toBe("");
    expect(extractFilePath("bash", "ok")).toBe("");
  });

  it("bash 带 Created 关键词", () => {
    expect(extractFilePath("bash", "Created: /tmp/out.txt\n")).toBe("/tmp/out.txt");
  });

  it("空 content 返回空", () => {
    expect(extractFilePath("write", "")).toBe("");
  });
});

describe("path helpers", () => {
  it("isImagePath / isMarkdownPath / isHtmlPath", () => {
    expect(isImagePath("/a/logo.png?x=1")).toBe(true);
    expect(isImagePath("/a/logo.PNG")).toBe(true);
    expect(isImagePath("/a/logo.bmp")).toBe(true);
    expect(isImagePath("/a/logo.txt")).toBe(false);
    expect(isMarkdownPath("/a/README.md")).toBe(true);
    expect(isMarkdownPath("/a/README.mdown")).toBe(true);
    expect(isMarkdownPath("/a/README.txt")).toBe(false);
    expect(isHtmlPath("/a/index.html")).toBe(true);
    expect(isHtmlPath("/a/index.htm")).toBe(true);
    expect(isHtmlPath("/a/index.md")).toBe(false);
  });

  it("detectLang：扩展名映射 + dockerfile 特判", () => {
    expect(detectLang("/a/main.py")).toBe("python");
    expect(detectLang("/a/app.tsx")).toBe("typescript");
    expect(detectLang("/a/x.sh")).toBe("bash");
    expect(detectLang("/a/Dockerfile")).toBe("dockerfile");
    expect(detectLang("/a/dockerfile.prod")).toBe("dockerfile");
    expect(detectLang("/a/unknown.zzz")).toBe("");
  });
});

describe("width/collapse persistence", () => {
  afterEach(() => localStorage.clear());

  it("clampWidth：下限 240 / 上限视口 45%", () => {
    expect(clampWidth(100)).toBe(240);
    expect(clampWidth(300, 1200)).toBe(300);
    expect(clampWidth(9999, 1200)).toBe(540);
  });

  it("storedWidth 默认 280；持久化后恢复", () => {
    expect(storedWidth()).toBe(280);
    persistWidth(320);
    expect(storedWidth()).toBe(320);
  });

  it("storedCollapsed 默认 false；持久化后恢复", () => {
    expect(storedCollapsed()).toBe(false);
    persistCollapsed(true);
    expect(storedCollapsed()).toBe(true);
  });

  it("非法持久化值回退默认", () => {
    localStorage.setItem("emrg.resultPanel.panelWidth", "abc");
    expect(storedWidth()).toBe(280);
  });
});
