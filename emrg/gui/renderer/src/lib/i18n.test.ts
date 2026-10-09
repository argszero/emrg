import { describe, expect, it, beforeEach } from "vitest";
import { DICTS, EN_DICT, ZH_DICT, detectLocale, getLocale, setLocale, t, LOCALE_KEY } from "./i18n";

/**
 * i18n.test.ts — 完整词典 + 原生逻辑移植测试（Batch 1 remainder）。
 * 断言镜像旧 emrg/gui/test/i18n.test.js（node:test → Vitest），
 * 并新增词典完整性守卫（394 keys zh/en 严格对齐 —— 防未来词典漂移）。
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
  // 两个绝对数是有意钉住的：新增/删除 key 必须在这里显式改一次，
  // 否则一次手滑删掉几条词条不会有人发现（下一行的对齐断言只要求 zh/en 一致，
  // 两边同时少一条它照样通过）。2026-09-23 由 `tool.pwsh.*`（Windows 方言，P8）从 397 改为 399；
  // 2026-09-30 由 `tool.detailName/-Input/-Output`（工具详情三段，rant 2026-09-30T09:17:54 → #1787）从 403 改为 406。
  // 2026-09-30 由 `composer.imageReason.*`（图片拒绝必须可见，rant 2026-09-30T09:35:04）从 403 改为 408。
  // 两个改动各加各的 key，互不重叠：合并后是 403 + 3 + 5 = 411（既不是 406 也不是 408）。
  // 2026-10-08 由 `composer.chooseImage`（选图入口，rant 2026-09-30T09:35:04 缺口 1）从 411 改为 412。
  // 2026-10-08 由 `composer.imageReasonClipboard`（系统给的图不受白名单所限，同 rant 要求 4）
  // 从 412 改为 413。
  // 2026-10-09 由 `composer.roots*`（3 条）+ `cmd.sandbox.hint`（1 条）+
  // `sandboxRoots.*`（12 条）——会话额外可写根的 GUI 入口与对话框，rant
  // 2026-10-09T09:43:39 Part 2(a)——从 413 改为 429。
  it("zh/en 各 429 个 key 且完全对齐", () => {
    const zhKeys = Object.keys(ZH_DICT);
    const enKeys = Object.keys(EN_DICT);
    expect(zhKeys.length).toBe(429);
    expect(enKeys.length).toBe(429);
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
      "tool.pwsh.doing",
      "chat.copyCode",
      "md.copyCode",
      "errorBoundary.title",
    ]) {
      expect(ZH_DICT[key]).toBeTruthy();
      expect(EN_DICT[key]).toBeTruthy();
    }
  });
});
