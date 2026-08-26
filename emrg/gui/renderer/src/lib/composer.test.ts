import { describe, expect, it } from "vitest";
import {
  CMD_MENU_CLOSED,
  clearQueued,
  menuForPrefix,
  menuNavigate,
  partitionRequeue,
  queueSend,
  steerCommitted,
  trackAfterResend,
} from "./composer";

/**
 * composer.test.ts — 补全菜单状态机 + P2 发送队列测试（Batch 2 remainder）。
 * 镜像 vanilla app.js：CommandMenu（480-526）/ keydown 导航（~1710-1735）/
 * queue-injection 协议（1450-1510，#655）。
 */
const identity = (k: string): string => k;

describe("menuForPrefix", () => {
  it("returns matching completions with index 0 (vanilla showCmdMenu)", () => {
    const m = menuForPrefix("/c", identity);
    expect(m.items.length).toBeGreaterThan(0);
    expect(m.items[0].cmd.startsWith("/c")).toBe(true);
    expect(m.index).toBe(0);
    expect(m.items.every((it) => it.cmd.startsWith("/c"))).toBe(true);
  });

  it("closes menu when no match (vanilla: items.length===0 → hideCmdMenu)", () => {
    expect(menuForPrefix("/zzz", identity)).toBe(CMD_MENU_CLOSED);
  });

  it("full command prefix narrows to the single command", () => {
    const m = menuForPrefix("/clear", identity);
    expect(m.items.map((it) => it.cmd)).toEqual(["/clear"]);
  });

  it("hint is resolved through the injected translate fn", () => {
    const t = (k: string): string => `[${k}]`;
    const m = menuForPrefix("/clear", t);
    expect(m.items[0].hint).toBe("[cmd.clear.hint]");
  });
});

describe("menuNavigate", () => {
  const t = identity;
  const m = menuForPrefix("/c", t);

  it("ArrowDown wraps forward (vanilla (index+1) % n)", () => {
    const next = menuNavigate(m, 1);
    expect(next.index).toBe(1);
    const wrapped = menuNavigate({ items: m.items, index: m.items.length - 1 }, 1);
    expect(wrapped.index).toBe(0);
  });

  it("ArrowUp wraps backward (vanilla (index-1+n) % n)", () => {
    const prev = menuNavigate({ items: m.items, index: 0 }, -1);
    expect(prev.index).toBe(m.items.length - 1);
    const prev2 = menuNavigate({ items: m.items, index: 2 }, -1);
    expect(prev2.index).toBe(1);
  });

  it("closed menu navigation is a no-op", () => {
    expect(menuNavigate(CMD_MENU_CLOSED, 1)).toBe(CMD_MENU_CLOSED);
    expect(menuNavigate(CMD_MENU_CLOSED, -1)).toBe(CMD_MENU_CLOSED);
  });

  it("is immutable — original state unchanged", () => {
    const next = menuNavigate(m, 1);
    expect(next).not.toBe(m);
    expect(m.index).toBe(0);
  });
});

describe("queueSend / steerCommitted (P2 queue-injection)", () => {
  it("queues a send when the session has no queue yet", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "hi" });
    expect(q.get("s1")).toEqual([{ requestId: "r1", text: "hi" }]);
  });

  it("appends to an existing session queue", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    queueSend(q, "s1", { requestId: "r2", text: "b" });
    expect(q.get("s1").map((e: { requestId: string }) => e.requestId)).toEqual(["r1", "r2"]);
  });

  it("keeps sessions isolated", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    queueSend(q, "s2", { requestId: "r2", text: "b" });
    expect(q.get("s2")).toEqual([{ requestId: "r2", text: "b" }]);
  });

  it("steer_committed removes the injected requestId and drops the key when empty", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    steerCommitted(q, "s1", "r1");
    expect(q.has("s1")).toBe(false);
  });

  it("steer_committed leaves other pending entries intact", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    queueSend(q, "s1", { requestId: "r2", text: "b" });
    steerCommitted(q, "s1", "r1");
    expect(q.get("s1").map((e: { requestId: string }) => e.requestId)).toEqual(["r2"]);
  });

  it("steer_committed for an unknown requestId is a no-op", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    steerCommitted(q, "s1", "nope");
    expect(q.get("s1").length).toBe(1);
  });
});

describe("partitionRequeue / trackAfterResend (queued_requeue)", () => {
  it("splits by the daemon-returned request_ids", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    queueSend(q, "s1", { requestId: "r2", text: "b" });
    queueSend(q, "s1", { requestId: "r3", text: "c" });
    const { toResend, remaining } = partitionRequeue(q, "s1", ["r1", "r3"]);
    expect(toResend.map((e) => e.requestId)).toEqual(["r1", "r3"]);
    expect(remaining.map((e) => e.requestId)).toEqual(["r2"]);
  });

  it("empty request_ids → nothing to resend, all remain", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    const { toResend, remaining } = partitionRequeue(q, "s1", []);
    expect(toResend).toEqual([]);
    expect(remaining.length).toBe(1);
  });

  it("missing session → empty split (daemon queue already dropped)", () => {
    const { toResend, remaining } = partitionRequeue(new Map(), "s1", ["r1"]);
    expect(toResend).toEqual([]);
    expect(remaining).toEqual([]);
  });

  it("trackAfterResend re-tracks when wasBusy (P2 #695 fix: first resend opens a new round)", () => {
    // 单客户端：首条重发开启新回合 → 必须重新入队，下个 queued_requeue 才能找到
    const remaining = [{ requestId: "r3", text: "c" }];
    const toResend = [
      { requestId: "r1", text: "a" },
      { requestId: "r2", text: "b" },
    ];
    const out = trackAfterResend(remaining, toResend, true);
    expect(out.map((e) => e.requestId)).toEqual(["r3", "r1", "r2"]);
  });

  it("trackAfterResend re-tracks subsequent items even when not busy (i>0)", () => {
    // wasBusy=false：首条（i=0）不再跟踪（若被注入则移除；未注入则静默丢），M2+ 必须跟踪
    const out = trackAfterResend([], [{ requestId: "r1", text: "a" }, { requestId: "r2", text: "b" }], false);
    expect(out.map((e) => e.requestId)).toEqual(["r2"]);
  });

  it("trackAfterResend without wasBusy and single item → nothing re-tracked", () => {
    const out = trackAfterResend([], [{ requestId: "r1", text: "a" }], false);
    expect(out).toEqual([]);
  });
});

describe("clearQueued", () => {
  it("drops the whole queue and reports existence (queued_cancelled)", () => {
    const q = new Map();
    queueSend(q, "s1", { requestId: "r1", text: "a" });
    expect(clearQueued(q, "s1")).toBe(true);
    expect(q.has("s1")).toBe(false);
  });

  it("returns false when nothing queued for the session", () => {
    expect(clearQueued(new Map(), "s1")).toBe(false);
  });
});
