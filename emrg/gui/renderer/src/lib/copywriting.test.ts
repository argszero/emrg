import { describe, expect, it } from "vitest";
import { buildCopy, createCopywriting, toolPhrases } from "./copywriting";
import type { TranslateFn } from "./utils";

/** 最小词典 + 计数翻译函数：验证 key 透传与参数插值 */
function makeT(counts: Record<string, number>): TranslateFn {
  const dict: Record<string, string> = {
    "tool.bash.doing": "运行中",
    "tool.bash.done": "已完成",
    "tool.fallback.doing": "处理中",
    "tool.fallback.done": "完成",
    "tool.failText": "操作失败",
    "copy.disconnected": "连接断开",
    "copy.growthCount": "已进化 {n} 次",
    "copy.aboutEvolution": "进化 {n} 次",
    "copy.aboutEvolutionEmpty": "尚未进化",
  };
  return (key, params) => {
    counts[key] = (counts[key] ?? 0) + 1;
    let s = dict[key] ?? key;
    if (params) {
      s = s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
    }
    return s;
  };
}

describe("toolPhrases", () => {
  it("known tool → doing/done phrases", () => {
    const t = makeT({});
    expect(toolPhrases("bash", t)).toEqual({ doing: "运行中", done: "已完成" });
  });
  it("missing tool → fallback phrases", () => {
    const t = makeT({});
    expect(toolPhrases(null, t)).toEqual({ doing: "处理中", done: "完成" });
    expect(toolPhrases("", t)).toEqual({ doing: "处理中", done: "完成" });
  });
});

describe("buildCopy", () => {
  it("builds the copy dict with interpolated functions", () => {
    const t = makeT({});
    const copy = buildCopy(t);
    expect(copy.disconnected).toBe("连接断开");
    expect(copy.growthCount(5)).toBe("已进化 5 次");
    expect(copy.aboutEvolution(3)).toBe("进化 3 次");
    expect(copy.aboutEvolution(0)).toBe("尚未进化");
  });
});

describe("createCopywriting", () => {
  it("exposes COPY, TOOL_FAIL_TEXT and refresh() rebuilds on locale switch", () => {
    const zhT = makeT({});
    const copy = createCopywriting(zhT);
    expect(copy.COPY.disconnected).toBe("连接断开");
    expect(copy.TOOL_FAIL_TEXT).toBe("操作失败");
    expect(copy.toolPhrases("bash").done).toBe("已完成");

    // locale 切换：注入不同 t 的实例 → refresh 重建（vanilla refresh() 语义）
    const enT: TranslateFn = (key) => (key === "copy.disconnected" ? "Disconnected" : key);
    const en = createCopywriting(enT);
    expect(en.COPY.disconnected).toBe("Disconnected");
    en.refresh();
    expect(en.COPY.disconnected).toBe("Disconnected");
  });
});
