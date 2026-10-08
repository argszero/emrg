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
  return fs.readFileSync(path.join(GUI_ROOT, rel), "utf8");
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
