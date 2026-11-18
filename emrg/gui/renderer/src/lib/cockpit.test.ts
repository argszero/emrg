import { afterAll, beforeAll, describe, expect, it } from "vitest";
import cssSource from "../cockpit.css?raw";
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
  formatRound,
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

  it("formatRound：未上报是 —；第 0 轮不存在", () => {
    expect(formatRound(null)).toBe("—");
    expect(formatRound(0)).toBe("—");
    expect(formatRound(1)).toBe("1");
    expect(formatRound(3.7)).toBe("3");
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
    const now = Date.now();
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

describe("图标轨与设置页的数值就是原型的数值（issue #2986）", () => {
  // 这些数值是宿主一眼能看出「不是同一个东西」的地方，所以它们必须被钉住，而不能只写在
  // 注释里。读的是样式表本身：`?raw` 拿的是文件文本，不经过 CSS 管线，也不会被 jsdom 的
  // 「不算布局」变成空话，前提是下面第一条先证明读到的不是空串。
  function block(selector: string): string {
    const at = cssSource.indexOf(`\n${selector} {`);
    expect(at, `${selector} 在样式表里找不到`).toBeGreaterThan(-1);
    const start = cssSource.indexOf("{", at) + 1;
    const end = cssSource.indexOf("}", start);
    return cssSource.slice(start, end);
  }

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

  it("设置页左导航 190px 定宽、纵向排列、项间距 1px", () => {
    const nav = block(".settings-nav");
    expect(nav).toContain("width: 190px");
    expect(nav).toContain("flex-direction: column");
    expect(nav).toContain("gap: 1px");
  });
});
