"use strict";
/**
 * atp_login.js — aitokenpool 的 GitHub 账号登录（issue #2774）。
 *
 * 设置页要能「点一次、浏览器授权、回来就配好了」。这里只放**不讲界面、不碰文件、不碰时间**
 * 的那部分：设备流的三个 HTTP 往返、服务端模型清单的读法，以及把结果并进 config.toml 文本
 * 的那一步。为什么在这里：renderer 是 sandbox + contextIsolation，没有网络权限（preload.js
 * 头注释），所以网络与写盘都归 main；而 main.js 只被 `node --check` 看、不被单测跑，于是逻辑
 * 落在这个纯模块里，main 只负责把 fs 与 fetch 接上。
 *
 * 三条硬约束，此处是它们唯一的实现点：
 *  - 探测不过就**一个字节都不写**：`completeLogin` 里 probe 在 writeConfig 之前，失败即返回；
 *  - `[llm]` 的其它键与其它段逐字保留：`applyAtpConfig` 只动 base_url / api_key / model 与
 *    对应那一条 `[[llm.models]]`，其余原样收发；
 *  - `[[llm.models]]` 已有同名条目时只更新 context_window，绝不追加第二条。
 *
 * 注入式设计：`fetchImpl` 由调用方给（生产是 main 的 `fetch`，测试是假上游），所以整条链
 * 在测试里不出网 —— issue #2774 验收 5 的阳性对照就是靠这一点做的。
 */

const { parse: parseToml, stringify: stringifyToml } = require("smol-toml");

/** 默认服务地址；`altBases()` 里的第一个是另一台部署。 */
const DEFAULT_BASE = "https://aitokenpool.args.fun";
const ALT_BASES = ["https://atp-dev.shenbiai.net"];

/** 登录后要直接可用的模型名（契约里服务端已上线的那一个）。 */
const READY_MODEL = "deepseek-flash";

/**
 * 服务地址归一：去尾部斜杠，去用户可能已经打进来的 `/v1`。
 * 不做这件事的后果是把 `…/v1` 拼成 `…/v1/v1`，而它的表现和「key 填错」一模一样。
 */
function normBase(base) {
  let s = String(base == null ? "" : base).trim();
  if (!s) return DEFAULT_BASE;
  s = s.replace(/\/+$/, "");
  s = s.replace(/\/v1$/, "");
  return s;
}

function endpoint(base, path) {
  return `${normBase(base)}${path}`;
}

/** 两个部署，默认在前；界面直接渲染它，不另存一份常量。 */
function bases() {
  return { default: DEFAULT_BASE, alternatives: ALT_BASES.slice() };
}

async function bodyOf(res) {
  try {
    const text = await res.text();
    if (!text) return {};
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function positive(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

/**
 * 服务端模型清单 → `[{ id, contextWindow }]`。
 *
 * 形状不唯一（OpenAI 风格 `{data:[…]}`、本仓风格 `{models:[…]}`、或干脆一个数组），
 * 所以三种都收；读不到就返回空数组——**空数组不等于 0**，调用方据此决定是否写
 * context_window，绝不拿一个猜出来的数覆盖用户原有配置。
 */
function readModels(payload) {
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray(payload && payload.data)
      ? payload.data
      : Array.isArray(payload && payload.models)
        ? payload.models
        : [];
  const out = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const id = String(item.id || item.name || item.model || "").trim();
    if (!id) continue;
    const raw =
      item.context_window !== undefined
        ? item.context_window
        : item.contextWindow !== undefined
          ? item.contextWindow
          : item.context_length !== undefined
            ? item.context_length
            : item.max_context_length !== undefined
              ? item.max_context_length
              : item.max_input_tokens;
    const n = Number(raw);
    out.push({ id, contextWindow: Number.isFinite(n) && n > 0 ? Math.trunc(n) : null });
  }
  return out;
}

/** 清单里那个模型的上限；没有条目、或条目没写，都回答 null（= 不写这个键）。 */
function contextWindowFor(models, model) {
  const hit = (models || []).find((m) => m && m.id === model);
  return hit && typeof hit.contextWindow === "number" ? hit.contextWindow : null;
}

/** `POST /api/auth/github/device/code` */
async function startDevice({ fetchImpl, base } = {}) {
  if (typeof fetchImpl !== "function") return { ok: false, error: "unreachable" };
  let res;
  try {
    res = await fetchImpl(endpoint(base, "/api/auth/github/device/code"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
  } catch {
    return { ok: false, error: "unreachable" };
  }
  const body = await bodyOf(res);
  if (res.status === 503) return { ok: false, error: "not_enabled", status: 503 };
  if (res.status !== 200) return { ok: false, error: "bad_response", status: res.status };
  const deviceCode = String(body.device_code || "");
  const userCode = String(body.user_code || "");
  const verificationUri = String(body.verification_uri || "");
  if (!deviceCode || !userCode || !verificationUri) {
    // 200 但没有可显示的东西不是成功：没有码就没有下一步可言。
    return { ok: false, error: "bad_response", status: 200 };
  }
  return {
    ok: true,
    deviceCode,
    userCode,
    verificationUri,
    expiresIn: positive(body.expires_in, 900),
    interval: positive(body.interval, 5),
  };
}

/**
 * 一步 `POST /api/auth/github/device/token`。
 *
 * 一次一问答，不在这里轮询：计时器归 renderer（它要能被取消、被界面看见），间隔归服务端
 * （`slow_down` 时按它给的数走；它没给就按规范 +5 秒，上限 60）。
 */
async function pollToken({ fetchImpl, base, deviceCode, currentInterval = 5 } = {}) {
  if (typeof fetchImpl !== "function") return { status: "unreachable" };
  if (!deviceCode) return { status: "bad_response", httpStatus: 0 };
  let res;
  try {
    res = await fetchImpl(endpoint(base, "/api/auth/github/device/token"), {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ device_code: deviceCode }),
    });
  } catch {
    return { status: "unreachable" };
  }
  const body = await bodyOf(res);
  if (res.status === 200) {
    const apiKey = String(body.api_key || "");
    if (!apiKey) return { status: "bad_response", httpStatus: 200 };
    return {
      status: "ok",
      apiKey,
      login: String(body.login || ""),
      userId: body.user_id == null ? "" : String(body.user_id),
    };
  }
  if (res.status === 202) return { status: "pending" };
  if (res.status === 429) {
    const given = positive(body.interval, 0);
    return { status: "slow_down", interval: given || Math.min(positive(currentInterval, 5) + 5, 60) };
  }
  if (res.status === 410) return { status: "expired" };
  if (res.status === 403) return { status: "denied" };
  return { status: "bad_response", httpStatus: res.status };
}

/** `GET /api/models` —— 一个新 key 唯一能证明自己可用的地方。 */
async function probeModels({ fetchImpl, base, apiKey } = {}) {
  if (typeof fetchImpl !== "function") return { ok: false, error: "unreachable" };
  let res;
  try {
    res = await fetchImpl(endpoint(base, "/api/models"), {
      method: "GET",
      headers: { authorization: `Bearer ${String(apiKey || "")}` },
    });
  } catch {
    return { ok: false, error: "unreachable" };
  }
  if (res.status !== 200) return { ok: false, status: res.status };
  return { ok: true, models: readModels(await bodyOf(res)) };
}

function entryMatches(entry, model) {
  if (!entry || typeof entry !== "object") return false;
  return entry.name === model || entry.model === model;
}

/**
 * config.toml 文本 → 登录后的 config.toml 文本。
 *
 * 只碰四样东西：`llm.base_url`、`llm.api_key`、`llm.model`，以及模型清单里那一条。
 * 其余键与其余段原样收发（要求 7）；同名条目只更新 context_window（要求 6）；
 * `contextWindow` 未知时不写这个键，也不拿默认值顶上去（顶上去就是把用户原来的数改坏）。
 */
function applyAtpConfig(text, { base, apiKey, model = READY_MODEL, contextWindow = null } = {}) {
  let toml;
  try {
    toml = text && String(text).trim() ? parseToml(String(text)) : {};
  } catch {
    return { ok: false, error: "config_unreadable" };
  }
  if (!toml || typeof toml !== "object" || Array.isArray(toml)) {
    return { ok: false, error: "config_unreadable" };
  }
  const next = { ...toml };
  const llm = { ...(next.llm || {}) };
  llm.base_url = `${normBase(base)}/v1`;
  llm.api_key = String(apiKey || "");
  llm.model = model;

  const models = Array.isArray(llm.models)
    ? llm.models.filter((m) => m && typeof m === "object").map((m) => ({ ...m }))
    : [];
  const known = typeof contextWindow === "number" && contextWindow > 0 ? Math.trunc(contextWindow) : null;
  const index = models.findIndex((m) => entryMatches(m, model));
  if (index >= 0) {
    if (known !== null) models[index].context_window = known;
  } else {
    models.push(known !== null ? { name: model, model, context_window: known } : { name: model, model });
  }
  llm.models = models;
  next.llm = llm;

  try {
    return { ok: true, text: stringifyToml(next) };
  } catch {
    return { ok: false, error: "config_unwritable" };
  }
}

/**
 * 整条落盘链，顺序本身就是要求：**先探测、后写入**。
 *
 * `readConfig`/`writeConfig` 由 main 用 fs 接上，所以这条路在测试里可以是内存里的字符串，
 * 也可以数「写了几次」——验收 2 与 5 要的正是这个调用次数，而不是「文件内容没变」。
 */
async function completeLogin({
  fetchImpl,
  base,
  apiKey,
  readConfig,
  writeConfig,
  model = READY_MODEL,
} = {}) {
  if (typeof readConfig !== "function" || typeof writeConfig !== "function") {
    return { ok: false, error: "bad_wiring" };
  }
  const probe = await probeModels({ fetchImpl, base, apiKey });
  if (!probe.ok) {
    return {
      ok: false,
      error: "probe_failed",
      status: probe.status == null ? 0 : probe.status,
      unreachable: probe.error === "unreachable",
    };
  }
  const contextWindow = contextWindowFor(probe.models, model);
  let text;
  try {
    text = await readConfig();
  } catch {
    return { ok: false, error: "config_unreadable" };
  }
  const merged = applyAtpConfig(text, { base, apiKey, model, contextWindow });
  if (!merged.ok) return { ok: false, error: merged.error };
  try {
    await writeConfig(merged.text);
  } catch {
    return { ok: false, error: "config_unwritable" };
  }
  return { ok: true, model, contextWindow, baseUrl: `${normBase(base)}/v1` };
}

module.exports = {
  DEFAULT_BASE,
  ALT_BASES,
  READY_MODEL,
  bases,
  normBase,
  readModels,
  contextWindowFor,
  startDevice,
  pollToken,
  probeModels,
  applyAtpConfig,
  completeLogin,
};
