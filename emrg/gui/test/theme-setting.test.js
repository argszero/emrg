"use strict";
/**
 * theme-setting.test.js — 外观主题只有**一个键**（issue #3013）。
 *
 * 原来的形状：GUI 写 `[gui] theme`（`system | light | dark`），TUI 读 `[client] theme`
 * （`auto | light | dark`）。于是宿主在 GUI 里选了浅色，TUI 启动时仍以为没人选过 —— 它去探测
 * 终端，探测不出就静默取深色。这里把映射与读写钉死，并且要求保存时**删掉**旧键：
 * 只写不删，保存一次就把两个键重新种回去。
 *
 * 读的那一半在 Python 侧：tests/test_tui_theme.py 用 `[client] theme = "light"` 断言 TUI
 * 读到的是浅色。两边各自钉住自己那一端，键名是同一条。
 */
const { test } = require("node:test");
const assert = require("node:assert");
const { parse: parseToml, stringify: stringifyToml } = require("smol-toml");

const ts = require("../theme_setting.js");

test("面板值 system 存成 auto：同一个选择的两种叫法，不是两个键", () => {
  assert.strictEqual(ts.themeForConfig("system"), "auto");
  assert.strictEqual(ts.themeForConfig("light"), "light");
  assert.strictEqual(ts.themeForConfig("dark"), "dark");
  // 认不出的值按 auto，与 emrg/config.py 读文件时同一条规则
  assert.strictEqual(ts.themeForConfig("chartreuse"), "auto");
  assert.strictEqual(ts.themeForConfig(undefined), "auto");
});

test("读：client.theme 优先，auto 显示为 system", () => {
  assert.strictEqual(ts.themeForUi({ client: { theme: "auto" } }), "system");
  assert.strictEqual(ts.themeForUi({ client: { theme: "light" } }), "light");
  assert.strictEqual(ts.themeForUi({ client: { theme: "dark" } }), "dark");
});

test("读：老安装的 gui.theme 仍然生效，不会被改名弄丢", () => {
  assert.strictEqual(ts.themeForUi({ gui: { theme: "light" } }), "light");
  assert.strictEqual(ts.themeForUi({ gui: { theme: "dark" } }), "dark");
  // 两个键都在时，client 是真源
  assert.strictEqual(ts.themeForUi({ client: { theme: "light" }, gui: { theme: "dark" } }), "light");
});

test("读：什么都没有、或键值不认识，就是跟随系统", () => {
  assert.strictEqual(ts.themeForUi({}), "system");
  assert.strictEqual(ts.themeForUi(undefined), "system");
  assert.strictEqual(ts.themeForUi({ client: { theme: "chartreuse" } }), "system");
  assert.strictEqual(ts.themeForUi({ gui: {} }), "system");
});

test("写：落到 client.theme，并且把旧的 gui.theme 删掉", () => {
  const toml = { llm: { model: "x" }, gui: { theme: "dark", lang: "zh" } };
  const changed = ts.writeTheme(toml, "light");

  assert.strictEqual(changed, true);
  assert.strictEqual(toml.client.theme, "light");
  assert.strictEqual("theme" in toml.gui, false, "旧键必须删掉：留着就还是两个键");
  assert.strictEqual(toml.gui.lang, "zh", "[gui] 里别的键一个都不许动");
  assert.strictEqual(toml.llm.model, "x", "[llm] 段一个键都不许动");
});

test("写：gui 段只剩 theme 时整段消失，不留一个空表", () => {
  const toml = { gui: { theme: "dark" } };
  ts.writeTheme(toml, "system");
  assert.strictEqual("gui" in toml, false);
  assert.strictEqual(toml.client.theme, "auto");
});

test("写：没有 gui 段也不会凭空造一个", () => {
  const toml = {};
  ts.writeTheme(toml, "dark");
  assert.deepStrictEqual(Object.keys(toml), ["client"]);
  assert.strictEqual(toml.client.theme, "dark");
});

test("写：值没变也没有旧键时报告「没动」，免得白写一次盘", () => {
  const toml = { client: { theme: "light" } };
  assert.strictEqual(ts.writeTheme(toml, "light"), false);
  assert.strictEqual(ts.writeTheme(toml, "dark"), true);
});

test("端到端：写出的文本被 TUI 那个读法认得（client.theme，取值在 auto/light/dark 里）", () => {
  // 真序列化一遍再解析回来：键路径与取值都必须活过 TOML 往返，否则 TUI 读到的是别的段。
  const toml = { llm: { model: "deepseek-v3" }, gui: { theme: "dark", lang: "zh" } };
  ts.writeTheme(toml, "light");
  const roundTripped = parseToml(stringifyToml(toml));

  assert.strictEqual(roundTripped.client.theme, "light");
  assert.strictEqual("gui" in roundTripped && "theme" in roundTripped.gui, false);
  assert.strictEqual(roundTripped.gui.lang, "zh");
  // TUI 侧 emrg/config.py 认这三个值；system 不是其中之一，所以必须已经被映射掉
  assert.ok(ts.CONFIG_MODES.includes(roundTripped.client.theme));
  assert.ok(!roundTripped.client.theme.includes("system"));
});
