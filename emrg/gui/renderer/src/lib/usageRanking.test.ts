import { describe, expect, it } from "vitest";
import {
  USAGE_SEGMENTS,
  hitRate,
  perCall,
  rankSegment,
  type UsageGroup,
} from "./usageRanking";

/**
 * usageRanking.test.ts — the arithmetic of one segment's ranking (rant 2026-11-20T17:30:50).
 *
 * The three rules the panel depends on are asserted here rather than in the panel, because
 * a panel test can see a row's text but not whether the row's bar was measured against the
 * right denominator:
 *
 * - the bar's denominator is **this segment's whole window**, not the largest row — a bar
 *   against the largest row reads as "the top session spent everything";
 * - the fourth column follows the segment (hit rate for input and cache, output per call
 *   for output), because one formula printed three times answers a question nobody asked;
 * - a row nothing measured sorts **last**: `null` is not zero, and printing "not measured"
 *   above a measured spend reports an absence as the day's largest cost.
 */

const groups: UsageGroup[] = [
  { session: "s_a", calls: 10, input: 600, cache: 300, output: 60 },
  { session: "s_b", calls: 5, input: 300, cache: 150, output: 30 },
  { session: "s_c", calls: 1, input: 100, cache: 50, output: 10 },
];

describe("rankSegment — 一段一张表", () => {
  it("占比条的分母是这一段的全部量，不是最大那一行", () => {
    const ranking = rankSegment(groups, "input", 1000);
    expect(ranking.rows.map((row) => row.share)).toEqual([0.6, 0.3, 0.1]);
    // 拿最大行当分母时第一行会是 1.0——这正是这条挡的读法
    expect(ranking.rows[0].share).toBeLessThan(1);
  });

  it("按当前这一段从大到小排；这一段一样大时按会话号，读两次印一个顺序", () => {
    expect(rankSegment(groups, "input", 1000).rows.map((row) => row.key)).toEqual([
      "s_a",
      "s_b",
      "s_c",
    ]);
    const tied: UsageGroup[] = [
      { session: "s_z", calls: 1, input: 50, cache: 0, output: 1 },
      { session: "s_m", calls: 1, input: 50, cache: 0, output: 1 },
    ];
    expect(rankSegment(tied, "input", 100).rows.map((row) => row.key)).toEqual(["s_m", "s_z"]);
  });

  it("这一段没测到的行排在最后，绝不排在最前", () => {
    const withHole: UsageGroup[] = [
      { session: "s_a", calls: 1, input: 100, cache: 0, output: 1 },
      { session: "s_b", calls: 1, input: null, cache: 5, output: null },
      { session: "s_c", calls: 1, input: 300, cache: 0, output: 1 },
    ];
    const ranking = rankSegment(withHole, "input", 400);
    expect(ranking.rows.map((row) => row.key)).toEqual(["s_c", "s_a", "s_b"]);
    // 没测到就是没测到：既不是 0，也不是一个占比
    expect(ranking.rows[2].tokens).toBeNull();
    expect(ranking.rows[2].share).toBeNull();
    // ……而它不进合计：合计只加测到的那些
    expect(ranking.listed).toBe(400);
  });

  it("第四列跟着段走：输入段与缓存段是命中率，输出段是每次调用平均输出", () => {
    const input = rankSegment(groups, "input", 1000);
    expect(input.rows[0].fourth).toBe(0.5); // 300 / 600
    const cache = rankSegment(groups, "cache", 500);
    expect(cache.rows.map((row) => row.fourth)).toEqual([0.5, 0.5, 0.5]);
    const output = rankSegment(groups, "output", 100);
    expect(output.rows[0].fourth).toBe(6); // 60 / 10 次调用
    // 一个公式不可能同时是这三个数：第四列绑在这一段上
    expect(output.rows[0].fourth).not.toBe(input.rows[0].fourth);
  });

  it("没有会话归属的记录不是一行：它被单列出来，也不进合计与排序", () => {
    const mixed: UsageGroup[] = [
      ...groups,
      { session: null, calls: 7, input: 400, cache: 0, output: 40 },
    ];
    const ranking = rankSegment(mixed, "input", 1400);
    expect(ranking.rows.map((row) => row.key)).toEqual(["s_a", "s_b", "s_c"]);
    expect(ranking.rows.some((row) => row.session === null)).toBe(false);
    expect(ranking.unnamed?.tokens).toBe(400);
    expect(ranking.unnamed?.calls).toBe(7);
    // 列出的 + 没有名字的 = 这一段的总量：页脚的等式在这一层已经成立
    expect((ranking.listed ?? 0) + (ranking.unnamed?.tokens ?? 0)).toBe(1400);
  });

  it("没有那一条「没名字」的记录时就不造一条：unnamed 是 null，不是 0", () => {
    const ranking = rankSegment(groups, "input", 1000);
    expect(ranking.unnamed).toBeNull();
    expect(ranking.listed).toBe(1000);
  });

  it("三段是按状态带的顺序排的：输入、缓存、输出", () => {
    expect(USAGE_SEGMENTS).toEqual(["input", "cache", "output"]);
  });

  it("命中率与平均输出的分母不成立时是 null，不是 0 或 Infinity", () => {
    expect(hitRate(null, 100)).toBeNull();
    expect(hitRate(100, null)).toBeNull();
    expect(hitRate(0, 0)).toBeNull();
    expect(perCall(100, 0)).toBeNull();
    expect(perCall(null, 4)).toBeNull();
    expect(perCall(100, 4)).toBe(25);
  });

  it("这一段总量没测到 → 每行的占比都是 null，不是 0%", () => {
    const ranking = rankSegment(groups, "input", null);
    expect(ranking.rows.map((row) => row.share)).toEqual([null, null, null]);
    // 而 token 仍然读得出来：总量没测到不等于这一段没测到
    expect(ranking.rows[0].tokens).toBe(600);
  });
});
