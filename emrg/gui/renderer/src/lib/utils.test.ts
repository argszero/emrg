import { describe, expect, it } from "vitest";
import { escapeHtml, formatMessageTime, genRequestId, messageTimeTitle, relTime, type TranslateFn } from "./utils";

/** 与 vanilla i18n 相同的 relTime 词典（zh）——断言与旧行为一致 */
const zhT: TranslateFn = (key, params) => {
  const dict: Record<string, string> = {
    "relTime.justNow": "刚刚",
    "relTime.minutesAgo": "{n} 分钟前",
    "relTime.hoursAgo": "{n} 小时前",
    "relTime.daysAgo": "{n} 天前",
  };
  const s = dict[key] ?? key;
  if (!params) return s;
  return s.replace(/\{(\w+)\}/g, (m, k) => (k in params ? String(params[k]) : m));
};

describe("escapeHtml", () => {
  it("escapes & < > \"", () => {
    expect(escapeHtml(`<b>&"`)).toBe("&lt;b&gt;&amp;&quot;");
  });
  it("coerces non-string input", () => {
    expect(escapeHtml(42)).toBe("42");
    expect(escapeHtml(null)).toBe("null");
  });
  it("leaves plain text untouched", () => {
    expect(escapeHtml("plain text 中文")).toBe("plain text 中文");
  });
});

describe("genRequestId", () => {
  it("returns a UUID-shaped string (v4)", () => {
    expect(genRequestId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });
});

describe("relTime", () => {
  it("returns empty string for missing/invalid input", () => {
    expect(relTime("", zhT)).toBe("");
    expect(relTime(null, zhT)).toBe("");
    expect(relTime(undefined, zhT)).toBe("");
    expect(relTime("not-a-date", zhT)).toBe("");
  });
  it("justNow under 1 minute", () => {
    expect(relTime(new Date().toISOString(), zhT)).toBe("刚刚");
  });
  it("minutes ago", () => {
    const iso = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(relTime(iso, zhT)).toBe("5 分钟前");
  });
  it("hours ago", () => {
    const iso = new Date(Date.now() - 2 * 3_600_000).toISOString();
    expect(relTime(iso, zhT)).toBe("2 小时前");
  });
  it("days ago", () => {
    const iso = new Date(Date.now() - 3 * 86_400_000).toISOString();
    expect(relTime(iso, zhT)).toBe("3 天前");
  });
});

describe("formatMessageTime", () => {
  it("读出记录自己写下的时钟（分秒小数与时区后缀都不影响）", () => {
    expect(formatMessageTime("2026-10-17T10:27:55.123456+08:00")).toBe("10:27");
    expect(formatMessageTime("2026-10-17T10:27:55")).toBe("10:27");
    expect(formatMessageTime("2026-10-17 10:27:55")).toBe("10:27");
  });

  it("不做时区换算：同一个字符串在两台不同时区的客户端上给出同一个值（与 TUI 同口径）", () => {
    // 若这里改成 `new Date(...)` 再取本地时分，同一个记录在不同时区的机器上就会显示不同的钟
    // ——那正是「时刻只来自 daemon」要杜绝的。
    expect(formatMessageTime("2026-10-17T10:27:55+00:00")).toBe("10:27");
    expect(formatMessageTime("2026-10-17T23:59:59Z")).toBe("23:59");
  });

  it("缺席或不可读 → 空串：不出现空括号、Invalid Date、NaN", () => {
    for (const bad of [undefined, null, "", "   ", "not a date", "2026-13-45T99:99", "10:27"]) {
      expect(formatMessageTime(bad as string | null | undefined)).toBe("");
    }
  });

  it("完整日期给悬停用；不可读同样是什么都没有", () => {
    expect(messageTimeTitle("2026-10-17T10:27:55.123456+08:00")).toBe("2026-10-17 10:27");
    expect(messageTimeTitle("")).toBe("");
    expect(messageTimeTitle(undefined)).toBe("");
  });
});
