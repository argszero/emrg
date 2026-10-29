"use strict";
/**
 * boot-contract.test.js — GUI 启动链路契约守卫（v0.2.81 断连回归，rant 2026-08-27T10:53:38）。
 *
 * v0.2.81 事故：React 迁移（Batch 5）丢掉了 `window.emrg.init()` 调用——renderer 只订阅
 * onEvent + sendMessage，从不调 init；而 main.js 的 ensureConnected()（唯一通向 daemon
 * websocket 的路径）只能由 emrg:init / saveSettings / scheduleReconnect 触发。init 不调 →
 * ensureConnected 永不执行 → GUI 永久断连（常显断连横幅），v0.2.81 发布后才被宿主发现。
 *
 * 修复（#1036，renderer 侧：DaemonBridgeProvider 挂载即调 window.emrg.init()，含运行时
 * 单测）恢复了链路。本守卫钉死 main.js + preload 两侧的静态契约——这两层没有任何运行时
 * 测试覆盖（renderer 测试全部 mock window.emrg；integration 测试直接打 daemon_client，
 * 不走 main.js boot 路径），防止未来重构静默破坏。renderer 侧（v0.2.81 事故点本体）除
 * #1036 运行时单测外，本守卫同样加静态钉死：即使未来重构删除运行时测试或绕过组件挂载
 * 路径，源码调用位也立即变红：
 *
 *   renderer 挂载（#1036 运行时测试 + 本守卫静态钉死）
 *     → window.emrg.init()
 *       → preload init → ipcRenderer.invoke("emrg:init")        ← 本守卫
 *         → main.js emrg:init handler → ensureConnected()        ← 本守卫
 *           → daemon websocket
 *
 * 断言均为静态正则（build-config.test.js 同款模式）：廉价、无运行时依赖、全 CI 步骤可跑。
 */

const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const GUI_ROOT = path.join(__dirname, "..");

function read(rel) {
  // Normalise the line ending here, once, so every assertion in this file reads
  // text whose newlines are "\n" whatever the checkout did. A sentinel written as
  // "\n  }\n" is a property of the file's content, not of the platform, but a CRLF
  // checkout makes indexOf answer -1 and the slice collapse to "" — measured
  // 2026-10-22: the same assertion passed on Linux and macOS and failed on Windows
  // with `actual: ''`. The assertions themselves are unchanged.
  return fs.readFileSync(path.join(GUI_ROOT, rel), "utf8").replace(/\r\n/g, "\n");
}

test("boot chain: main.js emrg:init handler must call ensureConnected()", () => {
  // 唯一通向 daemon websocket 的引导路径。若未来重构把 ensureConnected 移出 init、
  // 改名或删除，静态守卫立即红——没有任何运行时测试覆盖 main 侧这条链。
  const main = read("main.js");
  const handler = main.match(/ipcMain\.handle\("emrg:init"[\s\S]*?\n\s*\}\);/);
  assert.ok(handler, "main.js must contain an emrg:init ipcMain.handle block");
  assert.match(
    handler[0],
    /ensureConnected\(\)/,
    "emrg:init handler must call ensureConnected() — this is the only renderer-driven " +
      "path to the daemon websocket (v0.2.81 regression: dropping renderer init dropped " +
      "the connection)."
  );
  // G34/G71/G112：config 缺失 / key 未配置的快速返回必须先于 ensureConnected 分支
  assert.match(
    handler[0],
    /configExists = fs\.existsSync\(configPath\(\)\)/,
    "emrg:init must check config existence first (config missing → no daemon spawn)."
  );
  assert.match(
    handler[0],
    /if \(!keyConfigured\)/,
    "emrg:init must gate ensureConnected on api_key_configured (unconfigured → return early)."
  );
});

test("boot chain: preload exposes init -> emrg:init IPC channel", () => {
  // preload 桥契约：init 方法必须映射到 emrg:init 频道（preload-api.test.js 已守卫存在性；
  // 这里钉死频道映射，防止改名错位）。
  const preload = read("preload.js");
  assert.match(
    preload,
    /init:\s*\(\)\s*=>\s*ipcRenderer\.invoke\("emrg:init"\)/,
    "preload.js must map init() → ipcRenderer.invoke(\"emrg:init\") — the channel name " +
      "is part of the boot contract."
  );
});

test("boot chain: ensureConnected must exist and be the daemon-connect entry", () => {
  // ensureConnected 本身必须存在（conn-manager 职责）且不是空实现——防止"调用保留、
  // 实现被掏空"的隐性回归。
  const main = read("main.js");
  assert.match(
    main,
    /function ensureConnected|ensureConnected\s*=|const ensureConnected/,
    "main.js must define ensureConnected (the daemon websocket entry point)."
  );
  const connManager = read("conn-manager.js");
  assert.match(
    connManager,
    /ensureConnected/,
    "conn-manager.js must expose ensureConnected — main.js delegates to it."
  );
});

test("boot chain: the window icon candidates resolve to the products they name", () => {
  // 不是 boot 链的一环，但同属"main.js 静态契约"这一族，而且形状与上面几条一样：源码里
  // 写着一件事，运行时不声不响地做另一件。
  //
  // rant 2026-08-11T17:37:03 的修复在两个载体各写了同一串路径：package.json 的 refs（#1958
  // 修正）与本函数的 source 候选。后者写少一级——从 emrg/gui/ 出发 `../packaging/assets/icon.png`
  // 落在不存在的 emrg/packaging/assets/icon.png，而产物在仓库根 packaging/assets/icon.png
  // （gen-assets.sh 写出的地方）。`candidates.find(fs.existsSync)` 静默跳过写错的路径，所以它
  // 一直返回 undefined：源码启动的窗口用 Electron 默认图标，而注释声称指向仓库产物。
  //
  // 断言量的是**路径算术**（照 main.js 自己写的字面量解析），不是拼写：只把字面量抄一遍的
  // 守卫在路径改对之前也会通过。
  const main = read("main.js");
  const fn = main.match(/function windowIconPath\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(fn, "main.js must define windowIconPath()");
  const candidates = [...fn[0].matchAll(/path\.join\(__dirname((?:\s*,\s*"[^"]*")+)\)/g)].map(
    (call) => [...call[1].matchAll(/"([^"]*)"/g)].map((part) => part[1])
  );
  assert.ok(
    candidates.length >= 2,
    `windowIconPath must list its candidates as path.join(__dirname, ...) calls (got ${candidates.length})`
  );
  const resolved = candidates.map((parts) => path.resolve(GUI_ROOT, ...parts));
  // packaged：extraResources 的 to:"icon.png" 落在 resources/icon.png，而打包版 main.js 在
  // resources/app/ —— 所以从 emrg/gui/ 读是 `../icon.png`。
  const packaged = path.resolve(GUI_ROOT, "..", "icon.png");
  assert.ok(
    resolved.includes(packaged),
    `the packaged candidate must resolve to resources/icon.png (${packaged}); got ${JSON.stringify(resolved)}`
  );
  // source：仓库根目录的 packaging/assets/icon.png —— gen-assets.sh 的产物，不是 emrg/ 下的副本。
  const repoRoot = path.resolve(GUI_ROOT, "..", "..");
  const source = path.join(repoRoot, "packaging", "assets", "icon.png");
  assert.ok(
    resolved.includes(source),
    `the source candidate must resolve to ${source} (gen-assets.sh's product), not to a copy ` +
      `under emrg/ — a path one level short is skipped silently by existsSync and the window ` +
      `falls back to Electron's default icon; got ${JSON.stringify(resolved)}`
  );
});

test("boot chain: renderer DaemonBridgeProvider must call window.emrg.init() on mount", () => {
  // v0.2.81 事故点本体（rant 2026-08-27T10:53:38）：React 迁移把 renderer 的 init 调用
  // 整个丢掉——main 侧 ensureConnected() 的唯一 renderer 驱动入口消失 → GUI 永不断连。
  // #1036 运行时单测覆盖行为；本守卫用静态正则钉死源码调用位：即使未来重构删除运行时
  // 测试或绕过组件挂载路径（如改由非组件模块直接建桥），也立即变红。
  const provider = read("renderer/src/components/DaemonBridgeProvider.tsx");
  assert.match(
    provider,
    /emrg\?\.init\?\.\(\)/,
    "DaemonBridgeProvider mount effect must call window.emrg.init() — this is the " +
      "renderer's only path to the daemon websocket (v0.2.81 regression dropped this call)."
  );
  // init 返回值必须融合进 bridge store：只调用不消费（调用被保留、结果被丢弃）同样
  // 造成 connected=false 假死——v0.2.81 症状的隐性变体。
  assert.match(
    provider,
    /bridge\.applyInit\(result\)/,
    "init() result must be fused into the bridge store via applyInit — calling init " +
      "and dropping the result leaves the GUI disconnected (v0.2.81 symptom)."
  );
  // Provider 必须真实挂载进 App 树：移除挂载点 = 移除整个 init 链（且运行时单测独立
  // render 组件，无法捕获此回归）。
  const app = read("renderer/src/App.tsx");
  assert.match(
    app,
    /import \{ DaemonBridgeProvider \} from "\.\/components\/DaemonBridgeProvider"/,
    "App.tsx must mount DaemonBridgeProvider — unmounting it drops the entire init chain."
  );
});

/**
 * The arrow function of an `ipcMain.handle("<channel>", …)` registration, read out of
 * main.js by brace-walking rather than by a lazy regex: the handler's own closing brace is
 * spelled `});` on the last line, so a regex that stops before that line truncates the
 * function it means to extract (measured while writing this guard).
 */
function handlerArrow(source, channel) {
  const at = source.indexOf(`ipcMain.handle("${channel}"`);
  if (at < 0) return null;
  const start = source.indexOf("async () => {", at);
  if (start < 0) return null;
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}" && --depth === 0) return source.slice(start, i + 1);
  }
  return null;
}

test("device flow: the emrg:githubConnectWeb handler forwards the frame's user", async () => {
  // The renderer's device-flow panel branches on `res.user` for the daemon's
  // already-authenticated short-circuit: `github_connect_web_start` answers
  // {ok, user, error:"already_authenticated"} with no code and no url, and the panel answers
  // "connected" from that field. What the renderer receives is what THIS handler returns —
  // and no renderer test can see it, because they all mock `window.emrg`, so a field dropped
  // here is invisible from both sides. It was dropped: an already-connected install that
  // pressed "Sign in with GitHub (browser)" was told
  // "GitHub connect failed: already_authenticated".
  //
  // Driven, not matched: the real arrow function is read out of main.js and called with a
  // stubbed connection, so only the boundary (the daemon's frame) is faked. A regex for
  // `user:` would bless the spelling of a line without asking whether the value came from the
  // frame at all — which is the shape of the defect.
  const arrow = handlerArrow(read("main.js"), "emrg:githubConnectWeb");
  assert.ok(arrow, "main.js must contain an emrg:githubConnectWeb ipcMain.handle arrow");
  const handler = new Function("requireConn", `return (${arrow});`);
  const withFrame = (frame) => handler(() => ({ sendCommandAndWait: async () => frame }))();

  const started = await withFrame({
    ok: true,
    code: "ABCD-1234",
    url: "https://github.com/login/device",
    user: null,
    error: null,
  });
  assert.deepStrictEqual(started, {
    ok: true,
    code: "ABCD-1234",
    url: "https://github.com/login/device",
    user: null,
    error: null,
  });

  const already = await withFrame({
    ok: true,
    code: null,
    url: null,
    user: "octocat",
    error: "already_authenticated",
  });
  assert.strictEqual(
    already.user,
    "octocat",
    "the daemon's short-circuit login must cross the bridge: without `user` the renderer's " +
      "`res.ok && res.user` branch cannot fire, and the panel reports a connect failure for " +
      "an install that is connected already."
  );
  assert.strictEqual(already.code, null, "a short-circuit carries no device code");
});

// ── issue #2484：GUI 打开会话前，必须先问 daemon「它在哪个项目」 ──
//
// main.js 这一层没有运行时测试（renderer 测试全 mock window.emrg，integration 测试
// 直接打 daemon_client），所以用与本文件同款的静态钉子。被钉住的是一条因果链：
// 本地 sessions_index.json 读不到 → 调用方 `|| os.homedir()` → 带 resume:false 的
// 预打开绕过 daemon 的幽灵会话校验（_handle_resume_session 只在 resume 时核对 cwd）
// → daemon 照单创建 `~/.emrg/sessions/<sid>/`，真会话（另一个项目下的同名 id）再也打不开。

function handlerRegion(src, name) {
  const start = src.indexOf(`ipcMain.handle("${name}"`);
  assert.ok(start >= 0, `${name} handler 必须存在于 main.js`);
  const next = src.indexOf("ipcMain.handle(", start + 10);
  return src.slice(start, next === -1 ? undefined : next);
}

test("issue #2484: emrg:switchSession asks the daemon before it opens a session", () => {
  const region = handlerRegion(read("main.js"), "emrg:switchSession");
  assert.match(
    region,
    /await askSessionCwd\(/,
    "switchSession must ask the daemon where the session lives: its own index lookup " +
      "answers null for a session it cannot place, and the caller then opens at the home directory.",
  );
  assert.match(
    region,
    /asked \|\| projectPath \|\| resolveSessionCwd\(sessionId\)/,
    "precedence must be daemon answer > the caller's agent > the local index guess, so a " +
      "session from another agent opens where it really lives rather than where we guessed.",
  );
});

test("issue #2484: the send path's pre-open asks the daemon first, and does so before opening", () => {
  const region = handlerRegion(read("main.js"), "emrg:sendMessage");
  const openAt = region.indexOf("openSession(sessionId, sessionCwd, { resume: false })");
  assert.ok(openAt > 0, "send path still pre-opens with resume:false (the window this guards)");
  const beforeOpen = region.slice(0, openAt);
  assert.match(
    beforeOpen,
    /await askSessionCwd\(/,
    "the ask must happen BEFORE the resume:false open: that open skips the daemon's ghost " +
      "check, so the cwd it carries is the cwd the session gets created at.",
  );
});

test("issue #2484: askSessionCwd asks, judges, and never falls back to the home directory", () => {
  const src = read("main.js");
  const start = src.indexOf("async function askSessionCwd(");
  assert.ok(start > 0, "askSessionCwd must exist");
  const body = src.slice(start, src.indexOf("\n  }\n", start) + 4);
  assert.match(body, /sendCommandAndWait\(\s*"resolve_session_cwd"/, "it must send the daemon's own command");
  assert.match(body, /pickSessionCwd\(frame, null\)/, "an answer that is not `index` must not become a fact");
  assert.match(body, /return null;/, "an unanswerable question returns null for the caller to decide");
  assert.ok(
    !/DEFAULT_CWD/.test(body.split("candidateCwd || DEFAULT_CWD")[1] || ""),
    "DEFAULT_CWD may be the candidate it sends, never the value it returns",
  );
});

test("issue #2490: emrg:taskCreate forwards `schedule`, and only when the payload carries it", () => {
  // `schedule` is a top-level field of a task record. This handler built its
  // `task_create` payload from a fixed destructured list, so the key was dropped on
  // the floor and the GUI could only ever create a one-frequency task — the daemon
  // accepted the field the whole time. No runtime test covers the main process, which
  // is why this guard reads the source: it asserts the payload is built from a value
  // it forwards, not from a list that omits the field.
  const src = read("main.js");
  const start = src.indexOf('ipcMain.handle("emrg:taskCreate"');
  assert.ok(start > 0, "main.js must contain an emrg:taskCreate handler");
  const body = src.slice(start, src.indexOf('\n    });\n', start) + 8);
  assert.match(
    body,
    /schedule/,
    "emrg:taskCreate must name `schedule` — dropping it means the GUI can never create " +
      "a task with time-of-day windows (issue #2490)."
  );
  assert.match(
    body,
    /"schedule" in \(payload \|\| \{\}\)/,
    "the key must be forwarded only when the payload carries it: an absent key means " +
      "'do not touch the field', which is what keeps a pre-existing task's windows."
  );
  assert.match(
    body,
    /sendCommandAndWait\("task_create",\s*fields/,
    "the forwarded value has to reach the daemon command, not sit in a local object."
  );
});

test("issue #2490: emrg:taskLimits reports the daemon's own minimum, never a literal", () => {
  // The renderer's interval floor is the daemon's MIN_INTERVAL. The only honest way to
  // give it to the GUI is to report it, so this handler must read the field out of the
  // daemon's frame — a number written here would be the second copy the issue removes.
  const src = read("main.js");
  const start = src.indexOf('ipcMain.handle("emrg:taskLimits"');
  assert.ok(start > 0, "main.js must contain an emrg:taskLimits handler");
  const body = src.slice(start, src.indexOf('\n    });\n', start) + 8);
  assert.match(body, /frame\.min_interval/, "the floor is read from the daemon's frame");
  assert.ok(
    !/\b60\b/.test(body),
    "no literal floor may live in this handler — that is the copy issue #2490 removes"
  );
});

// issue #2637：切会话不再因「当前会话自有流运行中」被拒。
// 那条拒绝成立过一个年代：切换当时会关闭旧连接，而 close() 对 ownStream 连接先发
// cancel 再断 ws（conn-manager.test.js 的 P6 正钉着「先 cancel 再 close」），于是放行
// 切换就等于静默杀掉正在跑的那一轮。此后切换改为不动旧连接（关闭是 emrg:closeSession
// 的事），每会话各有 ws 与记录桶 ⇒ 那条拒绝只剩拒绝本身，而它的两个调用方都把拒绝说
// 在用户看不见的地方：侧边栏没有 catch（未处理的 promise 拒绝、界面毫无反应），任务
// 面板的 catch 写进「正在离开的」那个会话的记录。
test("issue #2637: emrg:switchSession does not consult ownStream to refuse a switch", () => {
  // 「查了没有」问的是代码，不是注释：注释里点到这把锁的正当改版必须静默，代码里真去
  // 读它必须变红 —— 两条都在下面各钉一次，否则这个谓词读不出任何东西。
  const withoutLineComments = (s) => s.replace(/^\s*\/\/.*$/gm, "");
  const region = handlerRegion(read("main.js"), "emrg:switchSession");
  assert.ok(
    !/ownStream/.test(withoutLineComments(region)),
    "a streaming turn lives on its own session's connection and outlives the switch, so " +
      "refusing the navigation protects nothing; the lock still matters to close(), which " +
      "cancels a busy connection — it just must not gate switching",
  );
  assert.ok(
    !/ownStream/.test(withoutLineComments("// 切会话不查 ownStream\nfoo();")),
    "a comment naming the lock is not a consultation of it",
  );
  assert.ok(
    /ownStream/.test(withoutLineComments('if (c?.ownStream) throw new Error("x");')),
    "reading it in code is still caught — else this guard reads nothing at all",
  );
  // 反方向：同一段里别的拒绝不许被一起带走。
  assert.match(region, /REJECT invalid session_id/, "session-id validation stays a refusal");
  assert.match(region, /too many open sessions/, "the open-session cap stays a refusal");
});
