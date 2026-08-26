import { describe, expect, it } from "vitest";
import { escapeHtml, genRequestId, relTime, type TranslateFn } from "./utils";

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
