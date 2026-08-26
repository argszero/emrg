import { describe, expect, it } from "vitest";
import { rewindPointRow, rewindPointsNewestFirst, type RewindPoint } from "./rewind";

/**
 * rewind.test.ts — /rewind 纯逻辑测试（Batch 4 slice 4）。
 * 镜像 vanilla showRewindDialog：行视图 + 倒序。
 */

describe("rewindPointRow", () => {
  it("label = #record_index，hint = preview（≤60 截断）", () => {
    const row = rewindPointRow({ record_index: 7, preview: "hello world" });
    expect(row).toEqual({ index: 7, label: "#7", hint: "hello world" });
  });

  it("preview 缺失 → 回退 content；两者皆缺 → 空串", () => {
    expect(rewindPointRow({ record_index: 1, content: "from content" }).hint).toBe("from content");
    expect(rewindPointRow({ record_index: 1 }).hint).toBe("");
  });

  it("preview 超过 60 字符截断（vanilla .slice(0, 60)）", () => {
    const long = "x".repeat(80);
    const row = rewindPointRow({ record_index: 2, preview: long });
    expect(row.hint).toHaveLength(60);
    expect(row.hint).toBe(long.slice(0, 60));
  });

  it("非字符串 preview 安全转字符串", () => {
    const row = rewindPointRow({ record_index: 3, preview: 123 as unknown as string });
    expect(row.hint).toBe("123");
  });
});

describe("rewindPointsNewestFirst", () => {
  it("倒序：最新 record_index 在最上", () => {
    const msgs: RewindPoint[] = [
      { record_index: 1 },
      { record_index: 2 },
      { record_index: 3 },
    ];
    expect(rewindPointsNewestFirst(msgs).map((m) => m.record_index)).toEqual([3, 2, 1]);
  });

  it("null/undefined → []（不抛错）", () => {
    expect(rewindPointsNewestFirst(null)).toEqual([]);
    expect(rewindPointsNewestFirst(undefined)).toEqual([]);
  });

  it("不修改入参数组（vanilla [...messages].reverse() 语义）", () => {
    const msgs: RewindPoint[] = [{ record_index: 1 }, { record_index: 2 }];
    rewindPointsNewestFirst(msgs);
    expect(msgs.map((m) => m.record_index)).toEqual([1, 2]);
  });
});
