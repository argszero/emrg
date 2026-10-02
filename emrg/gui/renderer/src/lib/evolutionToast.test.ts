import { describe, expect, it } from "vitest";
import {
  EVO_TOAST_DATE_KEY,
  claimEvolutionToastDay,
  evolutionToastDay,
  safeLocalStorage,
} from "./evolutionToast";

/**
 * evolutionToast.test.ts — 「进化完成」提示的**节流规则**。
 *
 * 这条规则的两个容易写错的地方都在这里钉住：判据是**本地日历日**（不是「距上次 N
 * 小时」），而存储**不可用时照常显示**（vanilla 的 catch 里只有一句 ignore）——
 * 后者尤其重要：把「读不到存储」当成「今天已经弹过」会让提示在某些宿主上**永不出现**，
 * 而那正是它存在的理由。
 */
describe("evolutionToast 节流规则", () => {
  it("今天没记过 → due，并把今天记下（vanilla 先记后弹）", () => {
    const mem = new Map<string, string>();
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    expect(claimEvolutionToastDay(storage, "2026-10-02")).toBe(true);
    expect(mem.get(EVO_TOAST_DATE_KEY)).toBe("2026-10-02");
  });

  it("同一天第二次 → 不 due（一天最多一次）", () => {
    const mem = new Map<string, string>();
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    expect(claimEvolutionToastDay(storage, "2026-10-02")).toBe(true);
    expect(claimEvolutionToastDay(storage, "2026-10-02")).toBe(false);
    expect(claimEvolutionToastDay(storage, "2026-10-02")).toBe(false);
  });

  it("隔一天 → 又是 due，且记的是新的一天", () => {
    const mem = new Map<string, string>([[EVO_TOAST_DATE_KEY, "2026-10-02"]]);
    const storage = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => void mem.set(k, v),
    };
    expect(claimEvolutionToastDay(storage, "2026-10-03")).toBe(true);
    expect(mem.get(EVO_TOAST_DATE_KEY)).toBe("2026-10-03");
  });

  it("存储拿不到（隐私模式/非浏览器）→ **照常显示**，不是静默关掉", () => {
    expect(claimEvolutionToastDay(null, "2026-10-02")).toBe(true);
    expect(claimEvolutionToastDay(null, "2026-10-02")).toBe(true);
  });

  it("存储读写抛异常 → 照常显示（vanilla 的 catch 里只有 ignore）", () => {
    const angry = {
      getItem: (): string | null => {
        throw new Error("blocked");
      },
      setItem: (): void => {
        throw new Error("blocked");
      },
    };
    expect(claimEvolutionToastDay(angry, "2026-10-02")).toBe(true);
  });

  it("日期拼写是 UTC 的日历日（与 vanilla 的 toISOString 同一把尺）", () => {
    expect(evolutionToastDay(new Date("2026-10-02T23:59:59Z"))).toBe("2026-10-02");
    expect(evolutionToastDay(new Date("2026-10-03T00:00:01Z"))).toBe("2026-10-03");
  });

  it("safeLocalStorage：jsdom 里读得到，读不到时回 null 而不是抛", () => {
    // jsdom 有 localStorage ⇒ 正常路径
    expect(safeLocalStorage()).toBe(window.localStorage);
    // 取值本身抛（不是方法调用）—— 这正是必须把**访问**也包进守卫的理由
    const desc = Object.getOwnPropertyDescriptor(window, "localStorage");
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      get() {
        throw new Error("blocked");
      },
    });
    try {
      expect(safeLocalStorage()).toBeNull();
    } finally {
      if (desc) Object.defineProperty(window, "localStorage", desc);
    }
    expect(safeLocalStorage()).toBe(window.localStorage);
  });
});
