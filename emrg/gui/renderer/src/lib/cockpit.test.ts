import { afterAll, beforeAll, describe, expect, it } from "vitest";
import cssSource from "../cockpit.css?raw";
import componentsSource from "../../css/components.css?raw";
import layoutSource from "../../css/layout.css?raw";
import {
  BODY_MIN,
  LEFT_COLUMN_DEFAULT,
  LEFT_COLUMN_MIN,
  LS_LEFT_WIDTH,
  RAIL_WIDTH,
  RIGHT_COLUMN_DEFAULT,
  clampWidth,
  columnBounds,
  countLabel,
  filterByName,
  findBodyWidthCaps,
  formatTokens,
  groupSize,
  mergeSessionRows,
  persistColumnWidth,
  resizedWidth,
  searchSessions,
  sessionHits,
  sortByName,
  storedColumnWidth,
  type SessionGroups,
  type SessionHit,
} from "./cockpit";
import type { OpenSessionEntry, SessionInfo } from "./sidebar";

/**
 * cockpit.test.ts — the shell's rules, without a DOM (issue #2959).
 *
 * Three subjects, and each is asserted in both directions where a one-sided test
 * would pass for the wrong reason:
 *
 * - **geometry**: a column's ceiling comes from the window, so the tests move the
 *   window rather than only the width;
 * - **the band's readings**: `null` is "not reported" and must not render as zero;
 * - **the body-width scanner**: a fixture that caps *and* a fixture that does not,
 *   and then the real stylesheets this app loads — because the scanner's own unit
 *   test proves the scanner, never the stylesheets (`a model of another module's rule
 *   is pinned by arming that module`).
 */

// Node 22+'s experimental localStorage shadows jsdom's and is unusable without
// `--localstorage-file`, so the two persistence tests get a memory implementation —
// the same one `resultPanel.test.ts` installs, for the same reason.
let savedLS: unknown;
const memLS = (() => {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
  };
})();
beforeAll(() => {
  savedLS = (globalThis as Record<string, unknown>).localStorage;
  (globalThis as Record<string, unknown>).localStorage = memLS;
});
afterAll(() => {
  (globalThis as Record<string, unknown>).localStorage = savedLS;
});

/* ── Geometry ─────────────────────────────────────────────── */

describe("column geometry", () => {
  it("列的 ceiling 由窗口减去其余固定开销与正文下限得出", () => {
    expect(columnBounds(1440, 400, LEFT_COLUMN_MIN).max).toBe(1440 - 400 - BODY_MIN);
  });

  it("窗口小到放不下时，ceiling 退回 min（不出现比 min 还小的上限）", () => {
    const b = columnBounds(500, 400, LEFT_COLUMN_MIN);
    expect(b.max).toBe(LEFT_COLUMN_MIN);
    expect(b.min).toBe(LEFT_COLUMN_MIN);
  });

  it("clampWidth：取整、夹在边界内；非有限值给 min", () => {
    const b = columnBounds(1440, 400, LEFT_COLUMN_MIN);
    expect(clampWidth(300.6, b)).toBe(301);
    expect(clampWidth(10, b)).toBe(LEFT_COLUMN_MIN);
    expect(clampWidth(99_999, b)).toBe(b.max);
    expect(clampWidth(Number.NaN, b)).toBe(LEFT_COLUMN_MIN);
  });

  it("resizedWidth 是「起点 + 位移」再过一次同样的夹取", () => {
    const b = columnBounds(1440, 400, LEFT_COLUMN_MIN);
    expect(resizedWidth(LEFT_COLUMN_DEFAULT, 36, b)).toBe(LEFT_COLUMN_DEFAULT + 36);
    expect(resizedWidth(LEFT_COLUMN_DEFAULT, -10_000, b)).toBe(LEFT_COLUMN_MIN);
  });

  it("宽度持久化：写进去读得回来；垃圾值回退默认（不把 NaN 当宽度）", () => {
    persistColumnWidth(LS_LEFT_WIDTH, 312);
    expect(storedColumnWidth(LS_LEFT_WIDTH, LEFT_COLUMN_DEFAULT)).toBe(312);
    localStorage.setItem(LS_LEFT_WIDTH, "wide");
    expect(storedColumnWidth(LS_LEFT_WIDTH, LEFT_COLUMN_DEFAULT)).toBe(LEFT_COLUMN_DEFAULT);
    localStorage.removeItem(LS_LEFT_WIDTH);
    expect(storedColumnWidth(LS_LEFT_WIDTH, LEFT_COLUMN_DEFAULT)).toBe(LEFT_COLUMN_DEFAULT);
  });

  it("右栏默认宽度与左栏默认宽度是两个数（收起一栏不忘掉另一栏）", () => {
    expect(RIGHT_COLUMN_DEFAULT).not.toBe(LEFT_COLUMN_DEFAULT);
  });
});

/* ── The band's readings ──────────────────────────────────── */

describe("band readings", () => {
  it("formatTokens：未上报是 —，不是 0", () => {
    expect(formatTokens(null)).toBe("—");
    expect(formatTokens(undefined)).toBe("—");
    expect(formatTokens(-1)).toBe("—");
    expect(formatTokens(Number.NaN)).toBe("—");
  });

  it("formatTokens：0 报 0（确实花了 0 与还没上报是两件事）", () => {
    expect(formatTokens(0)).toBe("0");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(1500)).toBe("1.5k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
  });

  it("countLabel 是「命中 / 总数」", () => {
    expect(countLabel(3, 12)).toBe("3 / 12");
    expect(countLabel(0, 0)).toBe("0 / 0");
  });
});

/* ── Ordering and search ──────────────────────────────────── */

describe("sortByName / filterByName", () => {
  it("按名字排序，大小写不敏感；不修改入参", () => {
    const rows = [{ name: "beta" }, { name: "Alpha" }, { name: "gamma" }];
    expect(sortByName(rows).map((r) => r.name)).toEqual(["Alpha", "beta", "gamma"]);
    expect(rows.map((r) => r.name)).toEqual(["beta", "Alpha", "gamma"]);
  });

  it("空查询保留全部；非空按包含匹配（大小写不敏感）", () => {
    const rows = [{ name: "emrg" }, { name: "aitokenpool" }];
    expect(filterByName(rows, "  ")).toHaveLength(2);
    expect(filterByName(rows, "TOKEN").map((r) => r.name)).toEqual(["aitokenpool"]);
  });

  it("自定义取值器：没有 name 字段的行也能被搜（rant 用时间/agent/正文）", () => {
    const rants = [
      { timestamp: "2026-11-18T10:00:00", agent: "emrg", message: "驾驶舱重做" },
      { timestamp: "2026-11-17T09:00:00", agent: "aitokenpool", message: "计费" },
    ];
    const key = (r: (typeof rants)[number]) => `${r.timestamp} ${r.agent} ${r.message}`;
    expect(filterByName(rants, "计费", key)).toHaveLength(1);
    expect(filterByName(rants, "2026-11-17", key)).toHaveLength(1);
    expect(filterByName(rants, "zzz", key)).toHaveLength(0);
  });
});

/* ── The search overlay's three groups ────────────────────── */

const HOUR = 3_600_000;

/**
 * A fixed instant at `hour`:`minute` local time, `daysAgo` days back.
 *
 * `searchSessions` buckets by **local calendar day** (`sameLocalDay` compares
 * `toDateString()`), so a fixture built as `Date.now() - HOUR` is "today" only while the
 * suite runs two hours past local midnight or later. Measured on CI: the `test` leg
 * started at 00:03 UTC on 2026-11-19, `now - 1*HOUR` fell on 11-18, and the assertion
 * read `expected [] to deeply equal [ 't2', 't1' ]` on a release PR whose only change
 * was version numbers. An anchor inside the day tests the grouping; it never tests the
 * hour the runner happens to start at.
 */
function localAt(daysAgo: number, hour: number, minute = 0): number {
  const d = new Date();
  d.setDate(d.getDate() - daysAgo);
  d.setHours(hour, minute, 0, 0);
  return d.getTime();
}

/** The middle of one local day — far enough from either midnight to hold ±2 hours. */
function localNoon(daysAgo = 0): number {
  return localAt(daysAgo, 12);
}

function hit(over: Partial<SessionHit> = {}): SessionHit {
  return { sid: "s1", title: "Alpha", agent: "emrg", at: Date.now(), open: false, ...over };
}

describe("sessionHits / searchSessions", () => {
  const open: OpenSessionEntry[] = [
    { sid: "s1", title: "打开的一", projectName: "emrg", lastActive: new Date(Date.now() - HOUR).toISOString() },
  ];
  const known: SessionInfo[] = [
    { session_id: "s1", title: "本地一" },
    { session_id: "s2", title: "历史的", agent: "aitokenpool" } as SessionInfo,
  ];

  it("打开的会话带 open 标记，且不会因为 daemon 也列了它就出现两次", () => {
    const hits = sessionHits(open, known);
    expect(hits.filter((h) => h.sid === "s1")).toHaveLength(1);
    expect(hits.find((h) => h.sid === "s1")?.open).toBe(true);
    expect(hits.find((h) => h.sid === "s2")?.open).toBe(false);
  });

  it("三组：打开中 / 今天 / 更早，组内按最近活动倒序", () => {
    const now = localNoon();
    const hits = [
      hit({ sid: "o1", open: true, at: now - 2 * HOUR }),
      hit({ sid: "o2", open: true, at: now - HOUR }),
      hit({ sid: "t1", at: now - 2 * HOUR }),
      hit({ sid: "t2", at: now - HOUR }),
      hit({ sid: "e1", at: now - 40 * 24 * HOUR }),
    ];
    const g = searchSessions(hits, "", now);
    expect(g.open.map((h) => h.sid)).toEqual(["o2", "o1"]);
    expect(g.today.map((h) => h.sid)).toEqual(["t2", "t1"]);
    expect(g.earlier.map((h) => h.sid)).toEqual(["e1"]);
    expect(groupSize(g)).toBe(5);
  });

  it("本地日期边界：「今天」是日历日，不是最近若干小时", () => {
    // Twenty minutes apart, and they belong to different groups: the rule the overlay
    // wears is the local calendar day, so a hit at 23:50 yesterday is "earlier" even
    // though the clock has barely moved. Stated here so the bucket has a name, and so a
    // fixture written as `now - HOUR` reads as the guess it is.
    const now = localAt(0, 0, 10); // 00:10 local
    const hits = [
      hit({ sid: "yesterday", at: localAt(1, 23, 50) }),
      hit({ sid: "today", at: localAt(0, 0, 5) }),
    ];
    const g = searchSessions(hits, "", now);
    expect(g.today.map((h) => h.sid)).toEqual(["today"]);
    expect(g.earlier.map((h) => h.sid)).toEqual(["yesterday"]);
    expect(g.open).toEqual([]);
  });

  it("查询命中标题、会话 id 与 agent 名三处；空查询保留全部", () => {
    const now = Date.now();
    const hits = [hit({ sid: "abc", title: "Alpha", agent: "emrg" }), hit({ sid: "xyz", title: "Beta", agent: "aitokenpool" })];
    expect(groupSize(searchSessions(hits, "", now))).toBe(2);
    expect(searchSessions(hits, "ALPHA", now).open.length + searchSessions(hits, "ALPHA", now).today.length).toBe(1);
    expect(searchSessions(hits, "xyz", now).today.map((h) => h.sid)).toEqual(["xyz"]);
    expect(searchSessions(hits, "aitoken", now).today.map((h) => h.sid)).toEqual(["xyz"]);
    expect(groupSize(searchSessions(hits, "nope", now))).toBe(0);
  });

  it("没有时间的行进「更早」，不假装是今天", () => {
    const g = searchSessions([hit({ sid: "x", at: 0 })], "", Date.now());
    expect(g.earlier.map((h) => h.sid)).toEqual(["x"]);
    expect(g.today).toHaveLength(0);
  });
});

/* ── Two listings, one set of rows (issue #2978) ──────────── */

/** Every sid in a grouping, whichever group it landed in. */
function sids(g: SessionGroups): string[] {
  return [...g.open, ...g.today, ...g.earlier].map((h) => h.sid);
}

describe("mergeSessionRows — the register's listing joins the active agent's", () => {
  const anHourAgo = new Date(Date.now() - HOUR).toISOString();
  const twoHoursAgo = new Date(Date.now() - 2 * HOUR).toISOString();

  // What the app state holds: the active agent's own listing. It is the copy that
  // predates the whole-register listing, so it names no agent and no directory.
  const activeAgent: SessionInfo[] = [
    { session_id: "s1", title: "本 agent 的", updated_at: anHourAgo },
  ];
  // What the daemon answers for every registered agent: the same s1 again, plus two
  // sessions no other source names.
  const everyAgent: SessionInfo[] = [
    { session_id: "s1", title: "本 agent 的", agent: "emrg", cwd: "/p/emrg", updated_at: anHourAgo },
    { session_id: "s2", title: "别人的", agent: "aitokenpool", cwd: "/p/atp", updated_at: twoHoursAgo },
    { session_id: "s3", title: "第三个", agent: "mem", cwd: "/p/mem", updated_at: twoHoursAgo },
  ];

  it("同一会话只留一行，且留下的是带 agent 名与目录的那一份", () => {
    const merged = mergeSessionRows(activeAgent, everyAgent);
    expect(merged.map((s) => s.session_id).sort()).toEqual(["s1", "s2", "s3"]);
    const s1 = merged.find((s) => s.session_id === "s1");
    expect(s1?.agent).toBe("emrg");
    expect(s1?.cwd).toBe("/p/emrg");
  });

  it("只在别的 agent 名下的会话也在，且每行都带 agent 名", () => {
    const hits = sessionHits([], mergeSessionRows(activeAgent, everyAgent));
    expect(hits).toHaveLength(3);
    expect(hits.map((h) => h.agent)).toEqual(["emrg", "aitokenpool", "mem"]);
  });

  it("只在 agent 名里出现的关键词命中它；标题与会话 id 各自也命中", () => {
    const hits = sessionHits([], mergeSessionRows(activeAgent, everyAgent));
    const now = Date.now();
    // 这个关键词不出现在任何标题或会话 id 里，只有 agent 名带着它。
    expect(sids(searchSessions(hits, "aitokenpool", now))).toEqual(["s2"]);
    expect(sids(searchSessions(hits, "第三个", now))).toEqual(["s3"]);
    expect(sids(searchSessions(hits, "s3", now))).toEqual(["s3"]);
    expect(groupSize(searchSessions(hits, "", now))).toBe(3);
  });

  it("空输入与残缺输入都不造出一行假会话", () => {
    expect(mergeSessionRows([], [])).toEqual([]);
    // 没有 session_id 的行不是会话：留着它就成了点不开的一行。
    const odd = [{ title: "无名" } as unknown as SessionInfo];
    expect(mergeSessionRows(odd, [])).toEqual([]);
  });
});

/* ── The body-width scanner ───────────────────────────────── */

describe("findBodyWidthCaps", () => {
  it("固定 px/rem 的 max-width 在正文选择器上就是封顶", () => {
    const caps = findBodyWidthCaps(".transcript-view { max-width: 760px; }");
    expect(caps).toEqual([{ selector: ".transcript-view", value: "760px" }]);
    expect(findBodyWidthCaps(".composer{max-width:48rem}")).toEqual([
      { selector: ".composer", value: "48rem" },
    ]);
  });

  it("不是封顶的三种写法都放过：none / 百分比 / calc 或 var", () => {
    expect(findBodyWidthCaps(".react-shell-main { max-width: none; }")).toEqual([]);
    expect(findBodyWidthCaps(".msg-body { max-width: 100%; }")).toEqual([]);
    expect(findBodyWidthCaps("#composer-wrap { max-width: calc(100% - 40px); }")).toEqual([]);
    expect(findBodyWidthCaps(".workspace { max-width: var(--w); }")).toEqual([]);
  });

  it("正文以外的选择器不管多窄都不算封顶（气泡、空态文案、对话框）", () => {
    expect(findBodyWidthCaps(".composer-card { max-width: 520px; }")).toEqual([]);
    expect(findBodyWidthCaps(".react-shell-main-note { max-width: 420px; }")).toEqual([]);
    expect(findBodyWidthCaps(".dialog { max-width: 520px; }")).toEqual([]);
  });

  it("注释里的 max-width 不算数（否则注释一条规则就变红）", () => {
    expect(findBodyWidthCaps("/* .msg-body { max-width: 700px } */\n.msg-body{max-width:100%}")).toEqual([]);
  });

  it("**真实的样式表**里没有任何一条封住正文的固定宽度", () => {
    // 走打包器自己的读法，不用 `node:fs`：渲染层的 tsconfig 只声明了
    // `vite/client` 与 jest-dom 两个类型库，`node:` 导入和 `process` 全局
    // 在干净安装上会让 `npm run typecheck` 直接红 —— 只有本机 `emrg/gui`
    // 恰好把 `@types/node` 提升上来时才看不见。`tests/test_renderer_node_types.py`
    // 钉住这条线，免得好不容易修掉的依赖再长回来。
    const sheets = import.meta.glob(["../*.css", "../../css/*.css"], {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const entries = Object.entries(sheets);
    // 名字先读出来：一个空列表会让下面那句 vacuously 通过
    expect(entries.length).toBeGreaterThan(2);
    // 两个目录都得读到 — 只有 src 命中时下面这句会红，说明 ../../css 那条 glob 没生效
    expect(entries.some(([file]) => !file.includes("css/"))).toBe(true);
    expect(entries.some(([file]) => file.includes("css/"))).toBe(true);
    const offenders: string[] = [];
    for (const [file, text] of entries) {
      for (const cap of findBodyWidthCaps(text)) {
        offenders.push(`${file}: ${cap.selector} { max-width: ${cap.value} }`);
      }
    }
    expect(offenders).toEqual([]);
  });
});

/* ── Slicing one rule out of a stylesheet ───────────────────── */

/**
 * One rule's declarations, sliced out of a sheet the bundler read as text.
 *
 * `?raw` rather than `node:fs` (the renderer typechecks with `vite/client` alone —
 * `tests/test_renderer_node_types.py` pins that), and the file's text rather than a
 * rendered assertion, because jsdom computes no layout: a rule that caps a height is
 * invisible to the DOM and plainly there in the source.
 *
 * The opening newline anchors the selector to the start of a line, so this cannot
 * answer with a rule that merely ends in the same name.
 */
function blockIn(source: string, selector: string): string {
  const at = source.indexOf(`\n${selector} {`);
  expect(at, `${selector} 在样式表里找不到`).toBeGreaterThan(-1);
  const start = source.indexOf("{", at) + 1;
  const end = source.indexOf("}", start);
  return source.slice(start, end);
}

/** The same read, for the sheet that describes the cockpit itself. */
const block = (selector: string) => blockIn(cssSource, selector);

describe("额外根目录：一个根能看见，两个根也必须能看见（issue #3168）", () => {
  /**
   * 一个根画成一棵树，两个根画进一个 `.ft-roots` 包装层（`FileTree.tsx`）。发布版给包装层
   * 的子项写了 `flex: 1 1 0`，而无单位的 `0` 是 **0px**，不是 `0%`：树于是成了基高为 0 的
   * 弹性项，高度只能来自一个自身高度由内容决定的纵列，偏偏 `.ft-body` 是滚动容器
   * （`overflow-y: auto`），主轴上的内在贡献是 0 —— 整条链归零，两棵树各 0 高，组体只剩
   * 包装层的 2px `gap`。宿主看到的就是「额外根目录」这一行在、下面什么都没有，连「没有」
   * 那行也没有（那行只在列表为空时画）。一个根的会话永远进不到这条路：它的 `.ft` 是
   * `.left-group-body`（block）的子项，`flex` 不生效，高度就是内容的高度。
   *
   * jsdom 不算布局，所以这条守卫读样式表文本；判据是在真布局引擎里量出来的，同样的规则、
   * 同样的三行 22px 目录（Chrome headless）：一个根无包装层 → 70px；两个根、`flex: 1 1 0`
   * → 0 / 0（组体 2px）；两个根、`flex: 1 1 auto` → 70 / 70（组体 142px）。
   */
  it("多根包装层不把子项钉在 0px 基准上", () => {
    expect(cssSource.length).toBeGreaterThan(2000);
    const shorthand = /flex:\s*([^;]+);/.exec(block(".ft-roots > .ft"))?.[1].trim() ?? "";
    expect(shorthand, "`.ft-roots > .ft` 里没有 flex 简写，这条守卫就无从谈起").not.toBe("");
    const basis = shorthand.split(/\s+/)[2] ?? "";
    expect(
      ["auto", "0%", "content"],
      `基准是「${basis}」：无单位的 0 是 0px，它让两个根各画成 0 高的空壳（实测 0/0）`,
    ).toContain(basis);
  });
});

describe("图标轨与设置页的数值就是原型的数值（issue #2986）", () => {
  // 这些数值是宿主一眼能看出「不是同一个东西」的地方，所以它们必须被钉住，而不能只写在
  // 注释里。读的是样式表本身：`?raw` 拿的是文件文本，不经过 CSS 管线，也不会被 jsdom 的
  // 「不算布局」变成空话，前提是下面第一条先证明读到的不是空串。

  it("样式表真的读到了（空串会让下面每一条断言都白白通过）", () => {
    expect(cssSource.length).toBeGreaterThan(2000);
    expect(cssSource).toContain(".icon-rail");
  });

  it("图标轨 60px 宽、内边距 14px 0 12px、列间距 4px", () => {
    // 宽度不是样式表声明的，是常量经 style 注入的（IconRail.tsx `style={{ width: RAIL_WIDTH }}`），
    // 所以这里钉常量、那里钉它真的落到元素上——两个载体各有一条，缺一条那一条就没人管。
    expect(RAIL_WIDTH).toBe(60);
    const rail = block(".icon-rail");
    expect(rail).toContain("padding: 14px 0 12px");
    expect(rail).toContain("gap: 4px");
    expect(rail).toContain("flex-direction: column");
  });

  it("标识 32×32、圆角 9px、底部外距 14px", () => {
    const mark = block(".rail-mark");
    expect(mark).toContain("width: 32px");
    expect(mark).toContain("margin-bottom: 14px");
    expect(block(".rail-mark svg")).toContain("border-radius: 9px");
  });

  it("按钮 40×40、圆角 11px；图标 19×19、线宽 1.65", () => {
    const item = block(".rail-item");
    expect(item).toContain("width: 40px");
    expect(item).toContain("height: 40px");
    expect(item).toContain("border-radius: 11px");
    // 40×40 只有在按钮不长大的前提下才是 40×40：`width`/`height` 管的是弹性盒算完之后
    // 的盒子，一条 `flex: 1` 就能让它们作废（实测 40 → 119，五个图标散满整根轨）。
    expect(item).toContain("flex: none");
    const icon = block(".rail-item svg");
    expect(icon).toContain("width: 19px");
    expect(icon).toContain("stroke-width: 1.65");
    expect(icon).toContain("fill: none");
  });

  it("选中态左外侧有 3px 宽、18px 高的竖条", () => {
    const bar = block(".rail-item.active::before");
    expect(bar).toContain("width: 3px");
    expect(bar).toContain("height: 18px");
    expect(bar).toContain("left: -7px");
  });

  it("提示气泡是自绘的：距按钮右边缘 8px、层级 40、不换行", () => {
    const tip = block(".rail-tip");
    expect(tip).toContain("calc(100% + 8px)");
    expect(tip).toContain("z-index: 40");
    expect(tip).toContain("white-space: nowrap");
    expect(tip).toContain("opacity: 0");
  });

  it("样式表真的读到了（空串会让下面每一条断言都白白通过）", () => {
    // `?raw` 在 `css: false` 下把样式表读成空串（`vite.config.ts` 为此把 css 打开），
    // 而「没有某条规则」这句话在一个不剩的样式表上永远成立。
    expect(layoutSource.length).toBeGreaterThan(1000);
    expect(layoutSource).toContain(".workspace-view");
  });

  it("layout.css 不留没有页面在用的选择器（旧规则会按类名命中下一个同名元素）", () => {
    // 样式表之间没有命名空间。`.side-nav-item { flex: 1 }` 在导航搬进 60px 图标轨之后
    // 仍然按类名命中新按钮，把五个 40×40 的盒子拉成 119px 高，图标于是散满整根轨。
    // 所以规则不是「删掉看得见的那一条」，而是：这里声明的每个名字，都必须真有一个
    // 页面在穿它。
    //
    // 源码走打包器读，不走 `node:fs`：渲染层只声明了 `vite/client` 与 jest-dom，
    // `node:` 导入会让 `npm run typecheck` 在干净安装上红（`tests/test_renderer_node_types.py` 钉着）。
    const sources = import.meta.glob(["../**/*.ts", "../**/*.tsx"], {
      query: "?raw",
      import: "default",
      eager: true,
    }) as Record<string, string>;
    const shipped = Object.entries(sources).filter(([file]) => !/\.test\.tsx?$/.test(file));
    // 先读名字：空列表会让下面那句 vacuously 通过
    expect(shipped.length).toBeGreaterThan(20);
    // 注释不是穿着者：`.session-header` 只活在一句注释里，把它算进来就等于替一条
    // 没人用的规则作证。
    const bare = (text: string) =>
      text
        .replace(/\/\*[\s\S]*?\*\//g, " ")
        .replace(/^\s*\/\/.*$/gm, " ")
        .replace(/\s\/\/[^\n]*/g, " ");
    const worn = shipped.map(([, text]) => bare(text)).join("\n");
    const declared = [
      ...new Set([...bare(layoutSource).matchAll(/[.#]([A-Za-z][\w-]*)/g)].map((m) => m[1])),
    ];
    expect(declared.length).toBeGreaterThan(10);
    const orphans = declared.filter(
      (name) => !new RegExp(`(^|[^\\w-])${name}([^\\w-]|$)`).test(worn),
    );
    expect(orphans).toEqual([]);
  });

  it("设置页左导航 190px 定宽、纵向排列、项间距 1px", () => {
    const nav = block(".settings-nav");
    expect(nav).toContain("width: 190px");
    expect(nav).toContain("flex-direction: column");
    expect(nav).toContain("gap: 1px");
  });

  // issue #3017：agent 页恒定一栏，不再随窗口宽度分栏；22px 原是列间距，一栏下变成行间距，收到 2px。
  it("agent 列表一栏：单列、行间距 2px，仍然是行不是卡片墙", () => {
    const grid = block(".agent-grid");
    expect(grid).toContain("grid-template-columns: minmax(0, 1fr)");
    expect(grid).toContain("gap: 2px");
    // 分栏规则必须真的不在了，不能只靠新规则「看起来更像」。
    expect(grid).not.toContain("auto-fill");
    // 「不是卡片」不能只靠类名还在不在来判：类还在，边框必须透明，否则一眼又变回卡片墙。
    expect(block(".agent-card")).toContain("border: 1px solid transparent");
  });

  // issue #3017：整页一条 720px 居中栏——标题、搜索框、列表、底部按钮共用它；其它页不受影响。
  it("agent 页一条 720px 居中栏，任务与 rant 页没有这条上限", () => {
    const body = block('.workspace-view[data-view="agents"] .workspace-view-body');
    expect(body).toContain("max-width: 720px");
    expect(body).toContain("margin-inline: auto");
    expect(body).toContain("width: 100%");
    expect(cssSource).not.toContain('.workspace-view[data-view="tasks"] .workspace-view-body');
    expect(cssSource).not.toContain('.workspace-view[data-view="rants"] .workspace-view-body');
    expect(cssSource).not.toContain(
      '.workspace-view[data-view="settings"] .workspace-view-body {\n  width: 100%',
    );
  });

  it("会话栏头部高 30px，底部一条分隔线", () => {
    const head = block(".session-col-head");
    expect(head).toContain("height: 30px");
    expect(head).toContain("border-bottom: 1px solid var(--border)");
  });

  it("Rant 表头与每一行同一套六列：92 / 64 / 58 / 106 / 1fr / 50px", () => {
    // 这两条规则在另一份样式表里（components.css），所以先证明读到的不是空串：
    // 一份读成空串的样式表会让下面每一条断言都白白通过。
    expect(componentsSource.length).toBeGreaterThan(2000);
    const six = "grid-template-columns: 92px 64px 58px 106px 1fr 50px";
    // 表头与行必须同一套列宽：只有一处对，列名就会和内容错位，而错位正是这张表要修的东西。
    expect(blockIn(componentsSource, ".rant-head")).toContain(six);
    expect(blockIn(componentsSource, ".rant-row")).toContain(six);
  });
});

/* ── One owner for the session column's height (issue #3087) ─── */

describe("会话栏的高度只有一处说了算（issue #3087）", () => {
  // 会话栏（`shell.css` 的 `.react-shell-session`）是 flex 列，里面只有两个子项：打开中的
  // 会话列表（`.session-list`）和下半的两段目录树（`.left-trees`）。两者都是 `flex: 1`，
  // 也就是按 0 基准平分整栏——除非有人在别处给其中一个再写一个高度上限。
  //
  // `css/layout.css` 的 `#open-sessions` 曾经写着 `max-height: 40%`。这个元素今天由
  // `SessionColumn.tsx` 渲染，同时带着 id 和 `.session-list` 类，而 id 选择器压得过类选择器，
  // 与两张样式表谁先加载无关：于是高度写在 `cockpit.css` 上、赢的却是旧规则，窗口越高，
  // 列表被截得越早，栏底空出来的那条也跟着变高。
  //
  // 规则因此不是「把 40% 调大」而是「一个元素的高度只有一处写」，三条断言各钉一个环节：
  // 旧规则不再写高度、高度写在类规则上、类规则也没有把上限换个地方重写一遍。
  // jsdom 不算布局，所以读的是样式表文本本身（`?raw`），第一条先证明读到的不为空串。

  it("旧规则不再定高：`#open-sessions` 没有高度上限，也不再自建滚动容器", () => {
    expect(layoutSource.length).toBeGreaterThan(1000);
    const old = blockIn(layoutSource, "#open-sessions");
    // 先确认拿到的是那条规则本身，而不是一段空文本——空片段会让下面每条 not 都白白通过
    expect(old).toContain("padding-bottom: var(--sp-1)");
    // 删掉的那个上限一旦回来，这里就红
    expect(old).not.toMatch(/max-height/);
    // 第二个毛病：滚动归 `.session-list` 管，两个滚动容器不是备份而是两层套子
    expect(old).not.toMatch(/overflow/);
    // 删的是高度，不是间距——间距被顺手带走也是回归
    expect(old).toContain("margin-bottom: var(--sp-1)");
  });

  it("高度写在类规则上，且没有换个选择器重写一遍：`.session-list` 是 flex: 1、无上限", () => {
    const list = block(".session-list");
    // 列表自己涨满整栏、并且仍然是那个滚动容器
    expect(list).toContain("flex: 1");
    expect(list).toContain("min-height: 0");
    expect(list).toContain("overflow-y: auto");
    // 把 40% 搬到这条类规则上不是修好，是换个文件名写同一件事
    expect(list).not.toMatch(/max-height/);
    // 列表涨上去就等于把两段树挤没：下半仍然要拿它那一半
    expect(block(".left-trees")).toContain("flex: 1");
  });
});

/**
 * 选区与当前行是两个状态，不能是同一个颜色（issue #3200 第 1 条）。
 *
 * jsdom 不算布局也解不出 `color-mix()`，所以这里读的是样式表文本本身（`?raw`）——调色板
 * 这条规则就住在样式表里，读它才是读这次修改本身。两条断言各钉一个方向：两者取值不同、
 * 选区的两个强度有先后。第一条先证明读到的是规则本身，而不是一段空串。
 */
describe("编辑器的选区不是当前行（issue #3200 第 1 条）", () => {
  it("当前行还是 hover 色，选区是强调色：两个状态两个颜色", () => {
    const activeLine = blockIn(cssSource, ".editor-body .cm-activeLine");
    // 选区那条规则列了两个选择器，`blockIn` 认的是以 `{` 收尾的那个拼法，所以读第二个；
    // 两个拼法同属一条规则，读到的是同一段声明。
    const selection = blockIn(cssSource, ".editor-body .cm-editor .cm-selectionBackground");
    // 空片段会让下面每条断言都白白通过
    expect(activeLine.length).toBeGreaterThan(20);
    expect(selection.length).toBeGreaterThan(20);
    expect(activeLine).toContain("var(--bg-hover)");
    // 同一个颜色就是这一条要修的毛病：选区画成了整行
    expect(selection).not.toContain("var(--bg-hover)");
    expect(selection).toContain("var(--accent)");
    // 当前行只声明 background-color：`background` 简写会把缩进参考线一起清掉
    expect(activeLine).toContain("background-color");
    expect(activeLine).not.toMatch(/(^|[;\s])background\s*:/);
  });

  it("有焦点时选区更重，不是一个色均匀到底", () => {
    const strength = (body: string): number => {
      const found = /(\d+)%/.exec(body);
      expect(found).not.toBeNull();
      return Number.parseInt((found as RegExpExecArray)[1], 10);
    };
    const idle = strength(
      blockIn(cssSource, ".editor-body .cm-editor .cm-selectionBackground"),
    );
    const focused = strength(
      blockIn(cssSource, ".editor-body .cm-focused .cm-editor .cm-selectionBackground"),
    );
    expect(focused).toBeGreaterThan(idle);
  });
});
