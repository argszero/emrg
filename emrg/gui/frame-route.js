"use strict";
/**
 * frame-route.js — 一条转发给渲染层的帧属于哪个会话。
 *
 * `main.js` 的 `connManager.onOpen(...)` → `conn.onEvent(...)` 桥把 daemon 的每一帧
 * 转发给渲染层，信封是 `{ type, data, sid }`，而渲染层正是按 `sid` 分桶的
 * （`daemonBridge.ts` 里 `KEY(sid)` 管着每个会话的 busy、计时和沙箱档位）。这里曾经
 * 无条件填「这条连接是为哪个会话打开的」——于是任何一帧只要送到这条连接上，就必然被
 * 画成当前会话自己的东西，没有任何一层能识别出来：别的会话的工具卡画进这个会话、
 * 开卡的那一帧到了而关卡的没到，卡片就永远转圈。
 *
 * 判别只有一条：**帧自己说了它属于哪个会话，就用它说的**。没说的帧是状态/全局帧，
 * 沿用连接的 sid —— 那一直是这类帧在这里的意思。这不是「不认识就丢掉」：丢帧只是把
 * 错误藏起来，让工具卡凭空消失，而不是让它落到正确的会话去。
 */

/**
 * @param {unknown} data 帧的 payload
 * @param {string|null|undefined} sid 这条连接是为哪个会话打开的
 * @returns {string|null} 这一帧应当落在哪个会话下
 */
function frameSessionId(data, sid) {
  if (data && typeof data === "object" && !Array.isArray(data)) {
    const own = data.session_id;
    // 空串不是「属于那个会话」：它没有名字，等于没说。非字符串同理（一个数字或对象
    // 落到 Map 的键上会成为一个谁也对不上的桶）。
    if (typeof own === "string" && own !== "") return own;
  }
  return sid ?? null;
}

module.exports = { frameSessionId };
