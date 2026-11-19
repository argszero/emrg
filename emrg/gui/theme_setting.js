"use strict";
/**
 * theme_setting.js — 外观主题在 config.toml 里的唯一一处读写（issue #3013）。
 *
 * 为什么单独成文件：主进程与 TUI 原先各存一个键、各用一套取值——
 *   * GUI 写 `[gui] theme`，取值 `system | light | dark`（main.js）；
 *   * TUI 读 `[client] theme`，取值 `auto | light | dark`（emrg/config.py + python_tui/theme.py）。
 * 于是宿主在 GUI 里选了浅色，TUI 启动时仍以为没人选过，于是走终端探测，探测不出就静默取深色。
 *
 * 现在只有一个键：`[client] theme`，取值 `auto | light | dark`（`emrg/config.py` 已经按这三个
 * 值读，TUI 已经在用）。GUI 的 `system` 就是 `auto`，只是同一个选择的另一种叫法——映射在这里，
 * 不散落在两个客户端里。
 *
 * `[gui] theme` 仍然被**读**（老安装的选择不会因为改名而丢），但不再被写：一旦写出，
 * 保存一次就等于把两个键重新种回去，而这正是本条要消灭的东西。
 */

/** TUI/配置侧的三个取值。 */
const CONFIG_MODES = ["auto", "light", "dark"];
/** GUI 设置面板的三个取值，`system` 与 `auto` 是同一件事。 */
const UI_MODES = ["system", "light", "dark"];

/**
 * `config.toml` 的 `[client] theme`（或老的 `[gui] theme`）→ 设置面板用的值。
 *
 * `system` 是缺省：没有键、键值不认识、读不到配置，都是「跟随系统」，而不是一个错误。
 */
function themeForUi(cfg) {
  const client = cfg && cfg.client ? cfg.client.theme : undefined;
  if (UI_MODES.includes(client)) return client === "auto" ? "system" : client;
  if (CONFIG_MODES.includes(client)) return client === "auto" ? "system" : client;
  const legacy = cfg && cfg.gui ? cfg.gui.theme : undefined;
  if (UI_MODES.includes(legacy)) return legacy;
  return "system";
}

/**
 * 设置面板的值 → 写进 `[client] theme` 的值。
 *
 * 认不出的值按 `auto` 处理，与 `emrg/config.py` 读文件时同一条规则：一个主题不值得让客户端
 * 起不来，也不值得让两端的读法分叉。
 */
function themeForConfig(uiValue) {
  if (uiValue === "light" || uiValue === "dark") return uiValue;
  return "auto";
}

/**
 * 把设置面板的选择落进已经解析好的 toml 对象，并清掉老键。
 *
 * 清掉是关键的一半：老安装里 `[gui] theme` 还在，只写不删就等于两个键继续并存，下一次
 * 「在任一端切换、另一端下次启动生效」又不成立。[gui] 段若因此变空，整段一起删。
 *
 * 返回值表示这次是否真的动了 toml，调用方据此决定要不要落盘。
 */
function writeTheme(toml, uiValue) {
  if (!toml || typeof toml !== "object") return false;
  const value = themeForConfig(uiValue);
  toml.client = toml.client && typeof toml.client === "object" ? toml.client : {};
  const changed = toml.client.theme !== value;
  toml.client.theme = value;
  let removed = false;
  if (toml.gui && typeof toml.gui === "object" && "theme" in toml.gui) {
    delete toml.gui.theme;
    removed = true;
    if (Object.keys(toml.gui).length === 0) delete toml.gui;
  }
  return changed || removed;
}

module.exports = { themeForUi, themeForConfig, writeTheme, CONFIG_MODES, UI_MODES };
