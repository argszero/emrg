// atp_login.test.js — aitokenpool 的 GitHub 设备流登录（issue #2774）
//
// 全部用假上游：`fetchImpl` 是注入的，所以这些用例一个请求都不出网
// （验收 5）。数的是**调用次数**——「写了几次」是这条链唯一的可观察事实，
// 「文件内容没变」证明不了什么（验收 2 的原话）。
const { test } = require("node:test");
const assert = require("node:assert");
const { parse: parseToml } = require("smol-toml");

const atp = require("../atp_login.js");

function res(status, body) {
  return {
    status,
    text: async () =>
      body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body),
  };
}

/** 按 URL 片段分派的假上游；未预期的 URL 直接炸，免得用例悄悄少走一步。 */
function fakeUpstream(routes) {
  const calls = [];
  const impl = async (url, init = {}) => {
    calls.push({ url, init });
    for (const [needle, handler] of Object.entries(routes)) {
      if (url.includes(needle)) return typeof handler === "function" ? handler(url, init) : handler;
    }
    throw new Error(`unexpected url: ${url}`);
  };
  impl.calls = calls;
  return impl;
}

function unreachableFetch() {
  return async () => {
    throw new Error("ECONNREFUSED");
  };
}

const CODE_OK = res(200, {
  device_code: "dev-1",
  user_code: "ABCD-1234",
  verification_uri: "https://aitokenpool.args.fun/device",
  expires_in: 900,
  interval: 5,
});

// ── 地址 ───────────────────────────────────────────────
test("the two deployments are named once, default first", () => {
  const { default: def, alternatives } = atp.bases();
  assert.strictEqual(def, "https://aitokenpool.args.fun");
  assert.deepStrictEqual(alternatives, ["https://atp-dev.shenbiai.net"]);
  assert.strictEqual(atp.normBase(""), def, "an empty base falls back to the default");
  assert.strictEqual(atp.normBase(" https://x.test/ "), "https://x.test");
  // 用户已经打进 /v1 时不能再拼一层，否则表现为「模型不回话」
  assert.strictEqual(atp.normBase("https://x.test/v1"), "https://x.test");
  assert.strictEqual(atp.normBase("https://x.test/v1/"), "https://x.test");
});

// ── 出码 ───────────────────────────────────────────────
test("a code request answers the five fields the dialog shows", async () => {
  const fetchImpl = fakeUpstream({ "/device/code": CODE_OK });
  const out = await atp.startDevice({ fetchImpl, base: "https://x.test" });
  assert.deepStrictEqual(out, {
    ok: true,
    deviceCode: "dev-1",
    userCode: "ABCD-1234",
    verificationUri: "https://aitokenpool.args.fun/device",
    expiresIn: 900,
    interval: 5,
  });
  assert.ok(fetchImpl.calls[0].url.startsWith("https://x.test/api/auth/github/device/code"));
});

test("503 says the deployment has the login switched off", async () => {
  const out = await atp.startDevice({
    fetchImpl: fakeUpstream({ "/device/code": res(503, { error: "github_login_not_enabled" }) }),
    base: "https://x.test",
  });
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.error, "not_enabled");
});

test("a 200 without a code is not a success", async () => {
  const out = await atp.startDevice({
    fetchImpl: fakeUpstream({ "/device/code": res(200, { device_code: "dev-1" }) }),
    base: "https://x.test",
  });
  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.error, "bad_response");
});

test("a dead service is reported, not thrown", async () => {
  const out = await atp.startDevice({ fetchImpl: unreachableFetch(), base: "https://x.test" });
  assert.deepStrictEqual(out, { ok: false, error: "unreachable" });
});

// ── 轮询的六种回答 ──────────────────────────────────────
test("poll: each server state maps to one answer, and none of them throws", async () => {
  const cases = [
    [res(202, { error: "authorization_pending" }), { status: "pending" }],
    [res(410, { error: "expired_token" }), { status: "expired" }],
    [res(403, { error: "access_denied" }), { status: "denied" }],
  ];
  for (const [response, expected] of cases) {
    const out = await atp.pollToken({
      fetchImpl: fakeUpstream({ "/device/token": response }),
      base: "https://x.test",
      deviceCode: "dev-1",
    });
    assert.deepStrictEqual(out, expected);
  }
});

test("poll: a 429 is honoured silently, at the interval the server names", async () => {
  const out = await atp.pollToken({
    fetchImpl: fakeUpstream({ "/device/token": res(429, { interval: 12 }) }),
    base: "https://x.test",
    deviceCode: "dev-1",
    currentInterval: 5,
  });
  assert.deepStrictEqual(out, { status: "slow_down", interval: 12 });
});

test("poll: a 429 without an interval backs off by five seconds, capped at a minute", async () => {
  const slow = await atp.pollToken({
    fetchImpl: fakeUpstream({ "/device/token": res(429, {}) }),
    base: "https://x.test",
    deviceCode: "dev-1",
    currentInterval: 5,
  });
  assert.deepStrictEqual(slow, { status: "slow_down", interval: 10 });
  const ceiling = await atp.pollToken({
    fetchImpl: fakeUpstream({ "/device/token": res(429, {}) }),
    base: "https://x.test",
    deviceCode: "dev-1",
    currentInterval: 58,
  });
  assert.strictEqual(ceiling.interval, 60);
});

test("poll: success carries the key and the GitHub login", async () => {
  const out = await atp.pollToken({
    fetchImpl: fakeUpstream({
      "/device/token": res(200, { api_key: "atp-key", user_id: 7, login: "argszero" }),
    }),
    base: "https://x.test",
    deviceCode: "dev-1",
  });
  assert.deepStrictEqual(out, {
    status: "ok",
    apiKey: "atp-key",
    login: "argszero",
    userId: "7",
  });
});

test("poll: a 200 with no key is not a success", async () => {
  const out = await atp.pollToken({
    fetchImpl: fakeUpstream({ "/device/token": res(200, { login: "argszero" }) }),
    base: "https://x.test",
    deviceCode: "dev-1",
  });
  assert.strictEqual(out.status, "bad_response");
});

// ── 探测 ───────────────────────────────────────────────
test("the probe reads the model list in either shape and leaves a bad key refused", async () => {
  const openai = await atp.probeModels({
    fetchImpl: fakeUpstream({
      "/api/models": res(200, { data: [{ id: "deepseek-flash", context_window: 200000 }] }),
    }),
    base: "https://x.test",
    apiKey: "k",
  });
  assert.deepStrictEqual(openai, { ok: true, models: [{ id: "deepseek-flash", contextWindow: 200000 }] });

  const houseStyle = await atp.probeModels({
    fetchImpl: fakeUpstream({
      "/api/models": res(200, { models: [{ name: "deepseek-flash", context_length: 131072 }] }),
    }),
    base: "https://x.test",
    apiKey: "k",
  });
  assert.deepStrictEqual(houseStyle.models, [{ id: "deepseek-flash", contextWindow: 131072 }]);

  const refused = await atp.probeModels({
    fetchImpl: fakeUpstream({ "/api/models": res(401, { error: "unauthorized" }) }),
    base: "https://x.test",
    apiKey: "bad",
  });
  assert.deepStrictEqual(refused, { ok: false, status: 401 });
  assert.ok(fakeUpstream({}).calls.length === 0);
});

test("a model the list does not name has no window, rather than a guessed one", () => {
  assert.strictEqual(atp.contextWindowFor([], "deepseek-flash"), null);
  assert.strictEqual(
    atp.contextWindowFor([{ id: "other", contextWindow: 8000 }], "deepseek-flash"),
    null,
  );
  assert.strictEqual(atp.contextWindowFor([{ id: "deepseek-flash", contextWindow: null }], "deepseek-flash"), null);
  assert.strictEqual(atp.contextWindowFor([{ id: "deepseek-flash", contextWindow: 200000 }], "deepseek-flash"), 200000);
});

// ── 合并进 config.toml ─────────────────────────────────
const PRESEEDED = `[llm]
base_url = "https://api.deepseek.com"
api_key = "old-key"
model = "deepseek-v3"
base_url_1 = "https://hand-added.test"
api_key3 = "hand-added-key"
context_window = 65536
vision = false

[[llm.models]]
name = "deepseek-v3"
model = "deepseek-v3"
context_window = 65536
vision = false

[gui]
theme = "dark"
lang = "zh"
`;

test("the write sets the three fields and leaves every other key and section alone", () => {
  const out = atp.applyAtpConfig(PRESEEDED, {
    base: "https://aitokenpool.args.fun",
    apiKey: "atp-key",
    model: "deepseek-flash",
    contextWindow: 200000,
  });
  assert.strictEqual(out.ok, true);
  const toml = parseToml(out.text);
  assert.strictEqual(toml.llm.base_url, "https://aitokenpool.args.fun/v1");
  assert.strictEqual(toml.llm.api_key, "atp-key");
  assert.strictEqual(toml.llm.model, "deepseek-flash");
  // 要求 7：手加的键、以及别的段，逐字还在
  assert.strictEqual(toml.llm.base_url_1, "https://hand-added.test");
  assert.strictEqual(toml.llm.api_key3, "hand-added-key");
  assert.strictEqual(toml.llm.context_window, 65536);
  assert.strictEqual(toml.llm.vision, false);
  assert.strictEqual(toml.gui.theme, "dark");
  assert.strictEqual(toml.gui.lang, "zh");
  // 原先的 deepseek-v3 条目没被吞掉
  const names = toml.llm.models.map((m) => m.name);
  assert.deepStrictEqual(names, ["deepseek-v3", "deepseek-flash"]);
});

test("an existing entry of the same name is updated in place, never duplicated", () => {
  const seeded = `[llm]
model = "deepseek-flash"

[[llm.models]]
name = "deepseek-flash"
model = "deepseek-flash"
context_window = 32768
vision = true
`;
  const out = atp.applyAtpConfig(seeded, {
    base: "https://x.test",
    apiKey: "atp-key",
    model: "deepseek-flash",
    contextWindow: 200000,
  });
  const models = parseToml(out.text).llm.models;
  assert.strictEqual(models.length, 1, "no second entry may appear");
  assert.strictEqual(models[0].context_window, 200000);
  assert.strictEqual(models[0].vision, true, "only the window moves");
});

test("an unknown window leaves the existing entry untouched and writes no key", () => {
  const seeded = `[llm]
model = "deepseek-flash"

[[llm.models]]
name = "deepseek-flash"
context_window = 32768
`;
  const same = parseToml(
    atp.applyAtpConfig(seeded, {
      base: "https://x.test",
      apiKey: "k",
      model: "deepseek-flash",
      contextWindow: null,
    }).text,
  ).llm.models;
  assert.strictEqual(same.length, 1);
  assert.strictEqual(same[0].context_window, 32768);

  const fresh = parseToml(
    atp.applyAtpConfig("[llm]\nmodel = \"deepseek-v3\"\n", {
      base: "https://x.test",
      apiKey: "k",
      model: "deepseek-flash",
      contextWindow: null,
    }).text,
  ).llm.models;
  assert.strictEqual(fresh.length, 1);
  assert.ok(!("context_window" in fresh[0]), "an unknown window is not invented");
});

test("a config that cannot be parsed is refused rather than overwritten", () => {
  const out = atp.applyAtpConfig("[llm\nmodel = ", { base: "https://x.test", apiKey: "k" });
  assert.deepStrictEqual(out, { ok: false, error: "config_unreadable" });
});

// ── 整条链的顺序 ────────────────────────────────────────
test("a probe that answers 200 writes exactly once, and the file carries the key", async () => {
  const writes = [];
  const out = await atp.completeLogin({
    fetchImpl: fakeUpstream({
      "/api/models": res(200, { data: [{ id: "deepseek-flash", context_window: 200000 }] }),
    }),
    base: "https://x.test",
    apiKey: "atp-key",
    readConfig: async () => PRESEEDED,
    writeConfig: async (text) => writes.push(text),
  });
  assert.strictEqual(out.ok, true);
  assert.strictEqual(out.model, "deepseek-flash");
  assert.strictEqual(out.contextWindow, 200000);
  assert.strictEqual(out.baseUrl, "https://x.test/v1");
  assert.strictEqual(writes.length, 1);
  const toml = parseToml(writes[0]);
  assert.strictEqual(toml.llm.api_key, "atp-key");
  assert.strictEqual(toml.llm.base_url_1, "https://hand-added.test");
});

test("a probe that is not 200 writes nothing at all", async () => {
  for (const response of [res(401, {}), res(500, {}), res(202, {})]) {
    const writes = [];
    const out = await atp.completeLogin({
      fetchImpl: fakeUpstream({ "/api/models": response }),
      base: "https://x.test",
      apiKey: "atp-key",
      readConfig: async () => PRESEEDED,
      writeConfig: async (text) => writes.push(text),
    });
    assert.strictEqual(out.ok, false);
    assert.strictEqual(out.error, "probe_failed");
    assert.strictEqual(writes.length, 0, `status ${response.status} must not write`);
  }
});

test("a service that cannot be reached writes nothing either", async () => {
  const writes = [];
  const out = await atp.completeLogin({
    fetchImpl: unreachableFetch(),
    base: "https://x.test",
    apiKey: "atp-key",
    readConfig: async () => PRESEEDED,
    writeConfig: async (text) => writes.push(text),
  });
  assert.deepStrictEqual(out, { ok: false, error: "probe_failed", status: 0, unreachable: true });
  assert.strictEqual(writes.length, 0);
});
