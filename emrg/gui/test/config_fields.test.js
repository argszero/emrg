"use strict";
/**
 * config_fields.test.js — the GUI's half of `config.toml` (issue #3221).
 *
 * The defect: `main.js` whitelisted five string keys, and the writer set only what survived — so a
 * field the form sent and the whitelist did not name was dropped with no error at all. These tests
 * pin the two properties that make that impossible now: a value of the wrong shape is **refused by
 * name**, and a refusal writes **nothing** (not even the fields that were fine).
 *
 * The list itself is compared against `emrg/config.py` by `tests/test_config_field_coverage.py`, on
 * the Python side, so a new field cannot arrive with no control. Here we only ask that what the
 * list says is what the module does.
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { parse: parseToml, stringify: stringifyToml } = require("smol-toml");

const cf = require("../config_fields.js");

test("每一个 config.py 字段都在这份清单里，且清单没有多余项", () => {
  const names = cf.FIELDS.map((f) => `${f.section}.${f.field}`);
  // 17 个字段：llm 13 + update 2 + sandbox 1 + client 1（emrg/config.py 的四个 dataclass）
  assert.strictEqual(cf.FIELDS.length, 17, `清单有 ${cf.FIELDS.length} 项`);
  for (const f of cf.FIELDS) {
    assert.ok(f.section && f.field, `条目缺 section/field: ${JSON.stringify(f)}`);
    assert.ok(f.control === "auto" || f.control === "custom" || f.control === "covered", `未知 control: ${f.control}`);
  }
  // 12 个此前没有入口的字段都在 auto/covered 里，而不是被记成 custom
  const drawn = cf.AUTO_FIELDS.map((f) => f.field).sort();
  assert.deepStrictEqual(drawn, [
    "auto_compact_threshold",
    "context_refresh_interval_ms",
    "context_window",
    "delay_minutes",
    "enabled",
    "max_tokens",
    "max_tool_rounds",
    "pwsh_path",
    "stream_options",
    "temperature",
    "vision",
  ]);
  assert.ok(names.includes("llm.vision_default"), "vision_default 必须有条目（covered 到 vision）");
});

test("整数：非数字与小数都被拒，且报出字段名", () => {
  const entry = cf.fieldByIpc("maxTokens");
  assert.deepStrictEqual(cf.coerce(entry, "4096"), { ok: true, value: 4096 });
  assert.deepStrictEqual(cf.coerce(entry, 1), { ok: true, value: 1 });

  const bad = cf.coerce(entry, "abc");
  assert.strictEqual(bad.ok, false);
  assert.match(bad.message, /\[llm\] max_tokens/, "拒绝要说清是哪个键");
  assert.match(bad.message, /"abc"/, "拒绝要把收到的值念出来");

  assert.strictEqual(cf.coerce(entry, "1.5").ok, false, "整数位不收小数");
  assert.strictEqual(cf.coerce(entry, "").ok, false, "空串不是 0");
  assert.strictEqual(cf.coerce(entry, 0).ok, false, "低于 min 被拒");
});

test("浮点与区间：越界被拒，边界被收", () => {
  const temp = cf.fieldByIpc("temperature");
  assert.deepStrictEqual(cf.coerce(temp, "0.7"), { ok: true, value: 0.7 });
  assert.strictEqual(cf.coerce(temp, 2, temp).ok, true, "上界本身合法");
  assert.strictEqual(cf.coerce(temp, "2.5").ok, false);

  const threshold = cf.fieldByIpc("autoCompactThreshold");
  assert.deepStrictEqual(cf.coerce(threshold, "0"), { ok: true, value: 0 }, "0 = 关，是合法值");
});

test("布尔与「用量开关」：值一律落到声明形状上", () => {
  assert.deepStrictEqual(cf.coerce(cf.fieldByIpc("vision"), "true"), { ok: true, value: true });
  assert.deepStrictEqual(cf.coerce(cf.fieldByIpc("vision"), undefined), { ok: true, value: false });

  const usage = cf.fieldByIpc("streamOptions");
  assert.deepStrictEqual(cf.coerce(usage, true), { ok: true, value: { include_usage: true } });
  assert.deepStrictEqual(cf.coerce(usage, false), { ok: true, value: { include_usage: false } });
});

test("一个字段被拒，则整份表单一个字节都不写", () => {
  const toml = { llm: { model: "x", max_tokens: 4096 } };
  const before = stringifyToml(toml);
  const out = cf.applyAll(toml, { maxTokens: "8192", temperature: "abc", updateEnabled: false });

  assert.strictEqual(out.ok, false);
  assert.strictEqual(out.field, "temperature", "拒的是温度，不是先出现的 max_tokens");
  assert.strictEqual(stringifyToml(toml), before, "被拒时文件必须原样：maxTokens 也不能写进去");
});

test("全部合法就都写进去，各自落到自己的段", () => {
  const toml = { llm: { model: "x" } };
  const out = cf.applyAll(toml, {
    maxTokens: "8192",
    temperature: "0.3",
    updateEnabled: false,
    updateDelayMinutes: "30",
    pwshPath: "C:/pwsh.exe",
  });

  assert.deepStrictEqual(out, { ok: true, changed: 5 });
  assert.strictEqual(toml.llm.max_tokens, 8192);
  assert.strictEqual(toml.llm.temperature, 0.3);
  assert.strictEqual(toml.update.enabled, false);
  assert.strictEqual(toml.update.delay_minutes, 30);
  assert.strictEqual(toml.sandbox.pwsh_path, "C:/pwsh.exe");
  assert.strictEqual(toml.llm.model, "x", "没提到的键一个都不许动");
});

test("写回后再解析，值仍在（内联表与数字都过一遍 smol-toml）", () => {
  const toml = {};
  cf.applyAll(toml, { streamOptions: true, autoCompactThreshold: "0.5", vision: true });
  const back = parseToml(stringifyToml(toml));
  assert.deepStrictEqual(back.llm.stream_options, { include_usage: true });
  assert.strictEqual(back.llm.auto_compact_threshold, 0.5);
  assert.strictEqual(back.llm.vision, true);
});

test("读：文件没写过的键答声明默认，写过的答文件里的值", () => {
  const entry = cf.fieldByIpc("maxTokens");
  assert.strictEqual(cf.readField({}, entry), 8192, "空文件 = 跑在默认值上，不是「未设置」");
  assert.strictEqual(cf.readField({ llm: { max_tokens: 4096 } }, entry), 4096);

  // 旧安装里 stream_options 可能是显式 null（对 Kimi 关掉），勾选框显示为未勾
  const usage = cf.fieldByIpc("streamOptions");
  assert.strictEqual(cf.readField({ llm: { stream_options: null } }, usage), false);
  assert.strictEqual(cf.readField({ llm: { stream_options: { include_usage: true } } }, usage), true);
});

test("值没变就不写：改动数说实话", () => {
  const toml = { llm: { max_tokens: 8192 } };
  const out = cf.applyAll(toml, { maxTokens: 8192 });
  assert.deepStrictEqual(out, { ok: true, changed: 0 });
});

test("每页都有控件：model 8、about 2、sandbox 1", () => {
  assert.strictEqual(cf.fieldsOnTab("model").length, 8);
  assert.deepStrictEqual(cf.fieldsOnTab("about").map((f) => f.field), ["enabled", "delay_minutes"]);
  assert.deepStrictEqual(cf.fieldsOnTab("sandbox").map((f) => f.field), ["pwsh_path"]);
  // 沙箱页此前一个控件都没有；现在这一页就是它的输入框
  assert.strictEqual(cf.fieldsOnTab("appearance").length, 0, "外观页的自有控件不在这份清单里");
});
