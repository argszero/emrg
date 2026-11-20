/**
 * One 24-hour window, ranked one segment at a time (rant 2026-11-20T17:30:50).
 *
 * The arithmetic lives here, not in the panel, for the reason every other `lib/` module
 * does: a ranking is the part a test can hold still. Three rules decide it, and each is a
 * mutation the suite asks for:
 *
 * - **the denominator is the segment's own total.** A bar drawn against the largest row
 *   instead of the window's whole reads as "the biggest session spent everything", and
 *   every other row's bar is then measured against the wrong thing.
 * - **the fourth column is per segment.** Cache hit rate for input and cache, output per
 *   call for output — one formula printed in all three answers a question the host did
 *   not ask.
 * - **the unnamed volume is not a row.** It has no session to open, so it is counted,
 *   reported beside the ranking and never sorted into it.
 */

export type UsageSegment = "input" | "cache" | "output";

/** The segments in the order the band shows them. */
export const USAGE_SEGMENTS: readonly UsageSegment[] = ["input", "cache", "output"];

/** One session's share of the window, as the daemon grouped it. */
export interface UsageGroup {
  session: string | null;
  calls: number;
  input: number | null;
  cache: number | null;
  output: number | null;
}

export interface RankingRow {
  /** The session id, or `""` for the row no session names. */
  key: string;
  session: string | null;
  tokens: number | null;
  /** This row's part of the segment's whole window, 0…1; `null` when nothing measured it. */
  share: number | null;
  /** The fourth column: cache hit rate, or output per call. */
  fourth: number | null;
  calls: number;
}

export interface SegmentRanking {
  segment: UsageSegment;
  /** The machine's whole segment for the window, or `null` when nothing measured it. */
  total: number | null;
  /** The named sessions, biggest first. */
  rows: RankingRow[];
  /** What no session names: reported, never ranked. */
  unnamed: RankingRow | null;
  /** The named rows' sum; plus `unnamed.tokens` this is `total`. */
  listed: number | null;
}

/** Cache hit rate: the reported hits over the prompt tokens they came out of. */
export function hitRate(input: number | null, cache: number | null): number | null {
  if (input === null || cache === null || input <= 0) return null;
  return cache / input;
}

/** Output per recorded call — the output segment's fourth column. */
export function perCall(output: number | null, calls: number): number | null {
  if (output === null || calls <= 0) return null;
  return output / calls;
}

function toRow(group: UsageGroup, segment: UsageSegment, total: number | null): RankingRow {
  const tokens = group[segment];
  return {
    key: group.session ?? "",
    session: group.session,
    tokens,
    share: tokens === null || total === null || total <= 0 ? null : tokens / total,
    fourth:
      segment === "output"
        ? perCall(group.output, group.calls)
        : hitRate(group.input, group.cache),
    calls: group.calls,
  };
}

function listedSum(rows: readonly RankingRow[]): number | null {
  const measured = rows
    .filter((row) => row.tokens !== null)
    .map((row) => row.tokens as number);
  return measured.length ? measured.reduce((a, b) => a + b, 0) : null;
}

/**
 * `groups` ranked by `segment`.
 *
 * A row whose segment nothing measured sorts **last**, not first: `null` is not zero, and
 * printing "not measured" above a measured spend would report an absence as the day's
 * largest cost. Ties break on the session id, so two readings of one window print one
 * order.
 */
export function rankSegment(
  groups: readonly UsageGroup[],
  segment: UsageSegment,
  total: number | null,
): SegmentRanking {
  const named = groups.filter((g) => typeof g.session === "string" && g.session !== "");
  const unnamedGroup = groups.find((g) => g.session === null) ?? null;
  const rows = named
    .map((group) => toRow(group, segment, total))
    .sort((a, b) => {
      if (a.tokens === null && b.tokens === null) return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
      if (a.tokens === null) return 1;
      if (b.tokens === null) return -1;
      if (a.tokens !== b.tokens) return b.tokens - a.tokens;
      return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
    });
  const unnamed =
    unnamedGroup && (unnamedGroup.calls > 0 || unnamedGroup[segment] !== null)
      ? toRow(unnamedGroup, segment, total)
      : null;
  return { segment, total, rows, unnamed, listed: listedSum(rows) };
}
