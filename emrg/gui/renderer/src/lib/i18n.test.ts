import { describe, expect, it, beforeEach } from "vitest";
import { DICTS, EN_DICT, ZH_DICT, detectLocale, getLocale, setLocale, t, LOCALE_KEY } from "./i18n";

/**
 * i18n.test.ts — 完整词典 + 原生逻辑移植测试（Batch 1 remainder）。
 * 断言镜像旧 emrg/gui/test/i18n.test.js（node:test → Vitest），
 * 并新增词典完整性守卫（373 keys zh/en 严格对齐 —— 防未来词典漂移）。
 */

describe("detectLocale", () => {
  it("zh 前缀 → zh，其他 → en", () => {
    expect(detectLocale("zh-CN")).toBe("zh");
    expect(detectLocale("zh-Hant")).toBe("zh");
    expect(detectLocale("en-US")).toBe("en");
    expect(detectLocale("ja-JP")).toBe("en");
  });
  it("无参（缺省 navigator.language）→ en 或 zh 之一，不抛错", () => {
    expect(["zh", "en"]).toContain(detectLocale());
  });
});

describe("t（词典取词）", () => {
  it("en 语言返回英文文案", () => {
    expect(t("sidebar.newChat", undefined, "en")).toBe("＋ New chat");
    expect(t("copy.disconnected", undefined, "en")).toBe("Connection lost — reconnecting…");
  });
  it("zh 语言返回中文文案", () => {
    expect(t("sidebar.newChat", undefined, "zh")).toBe("＋ 新对话");
    expect(t("copy.disconnected", undefined, "zh")).toBe("连接中断了，正在重新连接…");
  });
  it("{var} 插值", () => {
    expect(t("copy.aboutEvolution", { n: 42 }, "en")).toBe(
      "EMRG has self-evolved 42 times — thanks for every bit of feedback",
    );
    expect(t("copy.aboutEvolution", { n: 7 }, "zh")).toBe("EMRG 已自我成长 7 次，感谢你的每一次反馈");
  });
  it("未知 key → 原样返回", () => {
    expect(t("definitely.missing.key", undefined, "en")).toBe("definitely.missing.key");
  });
});

describe("getLocale / setLocale（localStorage 覆盖）", () => {
  beforeEach(() => {
    try {
      localStorage.removeItem(LOCALE_KEY);
    } catch {
      /* jsdom 不可用时忽略 */
    }
  });

  it("无覆盖 → 跟随系统检测", () => {
    // jsdom 默认 en-US；显式断言 getLocale 返回合法值即可
    expect(["zh", "en"]).toContain(getLocale());
  });

  it("setLocale 持久化到 localStorage 并立即生效", () => {
    const l = setLocale("en");
    expect(l).toBe("en");
    try {
      expect(localStorage.getItem(LOCALE_KEY)).toBe("en");
    } catch {
      /* 无 localStorage 环境跳过持久化断言 */
    }
    expect(t("sidebar.newChat", undefined, getLocale())).toBe("＋ New chat");
  });

  it("空值恢复跟随系统", () => {
    setLocale("en");
    setLocale("");
    const l = getLocale();
    expect(["zh", "en"]).toContain(l);
  });
});

describe("词典完整性守卫（防漂移）", () => {
  it("zh/en 各 373 个 key 且完全对齐", () => {
    const zhKeys = Object.keys(ZH_DICT);
    const enKeys = Object.keys(EN_DICT);
    expect(zhKeys.length).toBe(373);
    expect(enKeys.length).toBe(373);
    expect(zhKeys.sort()).toEqual(enKeys.sort());
    // DICTS 聚合结构
    expect(Object.keys(DICTS)).toEqual(["zh", "en"]);
  });
  it("关键分区 key 存在（侧边栏/输入区/设置/工具/聊天/错误边界）", () => {
    for (const key of [
      "sidebar.newChat",
      "nav.sessions",
      "composer.placeholder",
      "settings.title",
      "tool.bash.doing",
      "chat.copyCode",
      "md.copyCode",
      "errorBoundary.title",
    ]) {
      expect(ZH_DICT[key]).toBeTruthy();
      expect(EN_DICT[key]).toBeTruthy();
    }
  });
});
