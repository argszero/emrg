import { describe, expect, it } from "vitest";
import { sessionFailure, sessionFailureText } from "./sessionFailure";
import { t } from "./i18n";

/**
 * sessionFailure.test.ts — 失败的会话操作该说哪一句（vanilla `app.js` 三个 catch）。
 *
 * 这些断言读的是**词典里的那条串**，不是写死的英文——否则「串没人读」这个缺陷
 * 会在测试里被复制一份（测试自己成为唯一的读者，宿主的界面仍是空的）。
 * 取串一律走 `t(key, …, "en")`，所以词典改了测试跟着改。
 */

const en = (k: string, p?: Record<string, unknown>) => t(k, p, "en");

describe("sessionFailure 的规则（vanilla app.js:690 / 905 / 930）", () => {
  it("switch：超限的原话 → app.tooManyOpenSessions，且**不带** {msg}", () => {
    // main 进程抛的原话：`too many open sessions (20) — close some first`
    const f = sessionFailure("switch", new Error("too many open sessions (20) — close some first"));
    expect(f.key).toBe("app.tooManyOpenSessions");
    expect(f.msg).toBeUndefined();
  });

  it("switch：超限的判据是大小写不敏感的子串（vanilla 用 /too many open sessions/i）", () => {
    expect(sessionFailure("switch", new Error("Too Many Open Sessions")).key).toBe("app.tooManyOpenSessions");
  });

  it("switch：普通失败 → app.switchFailed，带原话", () => {
    const f = sessionFailure("switch", new Error("invalid session_id"));
    expect(f.key).toBe("app.switchFailed");
    expect(f.msg).toBe("invalid session_id");
  });

  it("new → app.newFailed；delete → app.deleteFailed（各自一句，不共用）", () => {
    expect(sessionFailure("new", new Error("boom")).key).toBe("app.newFailed");
    expect(sessionFailure("delete", new Error("boom")).key).toBe("app.deleteFailed");
  });

  it("抛出物不是 Error 也能读出一句话（vanilla 读 e.message，这里也要有兜底）", () => {
    expect(sessionFailure("delete", "plain string").msg).toBe("plain string");
    expect(sessionFailure("delete", null).msg).toBe("null");
  });

  it("渲染出来的是词典里那句：超限那句**不该**留下没被替换的 {msg}", () => {
    const cap = sessionFailureText(en, "switch", new Error("too many open sessions (20)"));
    expect(cap).toBe(en("app.tooManyOpenSessions"));
    expect(cap).not.toContain("{msg}");
    // 反面：超限不能渲染成通用那句（它要宿主做的事不一样：先关掉几个，而不是重试）
    expect(cap).not.toBe(en("app.switchFailed", { msg: "too many open sessions (20)" }));
  });

  it("渲染普通失败：词典模板 + 原话插值", () => {
    expect(sessionFailureText(en, "delete", new Error("daemon down"))).toBe(
      en("app.deleteFailed", { msg: "daemon down" }),
    );
    expect(sessionFailureText(en, "new", new Error("no project"))).toContain("no project");
  });

  it("三种操作的句子互不相同（一句话有两个家的那一天，就是它们开始漂移的那一天）", () => {
    const keys = ["switch", "new", "delete"].map(
      (op) => sessionFailure(op as "switch" | "new" | "delete", new Error("x")).key,
    );
    expect(new Set(keys).size).toBe(3);
  });
});
