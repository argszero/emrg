"use strict";
/**
 * chat.js — 聊天渲染：用户气泡 / EMRG ✦ 消息 / 流式 delta / 工具友好行 / 系统消息。
 * 事件类型与 main 契约不变（message_delta/done/tool_started/tool_finished/…）。
 */

const Chat = (() => {
  // P3（rant 15:07:19）：会话级状态隔离——每会话一份 groupNodes/toolRows/doneRids。
  // sid=null 为旧版单会话桶（无 sid 事件/旧调用方 → 行为与改造前完全一致）。
  // 容器路由：registerContainer(sid, el) 后该 sid 渲染进独立容器；未注册 → 默认 $("workspace")
  // （P4 openSessions 前实际只存在一个激活会话，容器切换随 P4 落地）。
  const sessionState = new Map(); // sid|null → { groupNodes, toolRows, doneRids }
  const containers = new Map(); // sid → 容器元素（P4 起每会话一个 .session-view）

  function st(sid) {
    const key = sid || null;
    if (!sessionState.has(key)) {
      sessionState.set(key, { groupNodes: new Map(), toolRows: new Map(), doneRids: new Set() });
    }
    return sessionState.get(key);
  }

  /** 该会话的渲染容器：已注册的独立容器优先，其次当前激活会话的容器（无 sid 事件落激活会话，P3 slice 2），最后回退默认聊天区（单会话过渡期行为）。 */
  function chatContainer(sid) {
    if (containers.has(sid)) return containers.get(sid);
    const active = App.state?.sessionId;
    if (active && containers.has(active)) return containers.get(active);
    return $("workspace");
  }

  /** P4 起：为新打开的会话注册独立容器；关闭时 unregister 清引用 */
  function registerContainer(sid, el) {
    containers.set(sid, el);
  }
  function unregisterContainer(sid) {
    containers.delete(sid);
    sessionState.delete(sid); // 会话关闭 → 释放其分组/工具行/已 done 状态
  }
  /** 该 sid 是否有**独立注册**容器（区别于 chatContainer 的回退链——断线标记只打真实容器） */
  function hasContainer(sid) {
    return containers.has(sid);
  }

  /** 复制代码按钮（设计 §3.3）：事件委托在聊天区，CSP 无内联 handler */
  function initCodeCopy() {
    const cv = $("workspace");
    cv.addEventListener("click", (e) => {
      const btn = e.target.closest(".code-copy");
      if (!btn) return;
      const pre = btn.closest(".code-block")?.querySelector("pre");
      if (!pre) return;
      const code = pre.textContent || "";
      const done = () => {
        btn.textContent = EMRG_Copy._t("chat.copied");
        setTimeout(() => { btn.textContent = EMRG_Copy._t("chat.copyCode"); }, 1500);
      };
      const fail = () => {
        btn.textContent = EMRG_Copy._t("chat.copyFailed");
        setTimeout(() => { btn.textContent = EMRG_Copy._t("chat.copyCode"); }, 1500);
      };
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(code).then(done, fail);
      } else {
        // 非安全上下文兜底：textarea 选中复制
        const ta = document.createElement("textarea");
        ta.value = code;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try {
          document.execCommand("copy");
          done();
        } catch {
          fail();
        }
        document.body.removeChild(ta);
      }
    });
  }
  initCodeCopy(); // 模块级绑定一次（boot 可重复调用，防 listener 泄漏）

  /** 追加节点到该会话聊天区并滚动 */
  function append(node, sid) {
    chatContainer(sid).appendChild(node);
    scrollToBottom(sid);
    // rant 14:11：任何消息增删都重新评估欢迎屏显隐（此前只在切会话时评估 → 首条消息后欢迎屏不隐藏）
    App.updateEmptyState?.();
  }

  function scrollToBottom(sid) {
    const cv = chatContainer(sid);
    cv.scrollTop = cv.scrollHeight;
  }

  function clear(sid) {
    const key = sid || null;
    const cv = chatContainer(key);
    // 清空消息节点但保留 .session-header 标题栏（app.js 每个 .session-view 顶部固定）
    for (const node of [...cv.children]) {
      if (node.classList && node.classList.contains("session-header")) continue;
      node.remove();
    }
    st(key).groupNodes.clear();
    st(key).toolRows.clear();
    st(key).doneRids.clear();
    App.updateEmptyState?.(); // rant 14:11：清空（切会话/新会话）也同步欢迎屏显隐
  }

  /** 用户消息：右对齐柔和气泡 */
  function addUserMessage(text, sid) {
    const node = el("div", { class: "msg user" }, text);
    append(node, sid);
    return node;
  }

  /** EMRG 消息：全宽 + ✦ 标识（流式节点，body 先 textContent 后整体 marked） */
  function createAssistantNode(isOwn, sid) {
    const node = el("div", { class: "msg assistant" });
    if (!isOwn) {
      node.appendChild(el("div", { class: "remote-label" }, EMRG_Copy._t("chat.fromOtherClient")));
    }
    const mark = el("span", { class: "msg-assistant-mark" }, "✦ ");
    const body = el("div", { class: "msg-body typing" });
    body.appendChild(mark);
    node.appendChild(body);
    append(node, sid);
    return node;
  }

  /** 系统消息（温和置中） */
  function addSystemMessage(text, sid) {
    append(el("div", { class: "msg system" }, text), sid);
  }

  /** Read-only history user message (rant 14:15:12: restore recent history on
   *  session switch; not interactive). Reuses the user-bubble style plus a
   *  history class; prepend inserts after the load bar keeping scroll pos. */
  function addHistoryMessage(text, sid, { prepend = false } = {}) {
    const node = el("div", { class: "msg user history" }, text);
    const cv = chatContainer(sid);
    if (prepend) {
      const bar = cv.querySelector(".history-load-bar");
      cv.insertBefore(node, bar ? bar.nextSibling : cv.firstChild);
    } else {
      cv.appendChild(node);
    }
    App.updateEmptyState?.();
    return node;
  }

  /** Top history load bar (rant 14:15:12): text=null removes it; otherwise
   *  it becomes the first child of the session container. */
  function setLoadBar(sid, text) {
    const cv = chatContainer(sid);
    let bar = cv.querySelector(".history-load-bar");
    if (text == null) {
      if (bar) bar.remove();
      return;
    }
    if (!bar) {
      bar = el("div", { class: "history-load-bar" });
      cv.insertBefore(bar, cv.firstChild);
    }
    bar.textContent = text;
  }

  /** 流式 delta（G122 main 已按 chunks 批量）——按会话隔离分组/已 done 集合 */
  function handleDelta(chunks, sid) {
    const { groupNodes, doneRids } = st(sid);
    for (const chunk of chunks) {
      const rid = chunk.request_id;
      if (!rid || doneRids.has(rid)) continue; // rant 14:11：已 done 的流丢弃残留 delta，不建孤儿节点
      let group = groupNodes.get(rid);
      if (!group) {
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn, sid);
        group = { node, nodes: [node], hasText: false, sealed: false };
        groupNodes.set(rid, group);
      } else if (group.sealed) {
        // rant 21:57:10：上一文本段被工具行"封存"→ 新文本段开新节点（旧节点保留在 DOM 原位）
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn, sid);
        group.node = node;
        group.nodes.push(node);
        group.sealed = false;
      }
      const content = chunk.content || "";
      if (content) group.hasText = true;
      const body = group.node.querySelector(".msg-body") || group.node;
      // rant 21:00:28：流式 markdown（块投影）——稳定块完整渲染并缓存 DOM（不闪烁/不打断选中），
      // 尾部 live 块只渲染已稳定部分；代码围栏未闭合 → 纯文本不高亮（TUI fence_count%2 启发式一致）。
      // 无 marked.lexer 或投影异常 → 回退既有纯文本追加（done 时整体渲染）。
      const stream = group.node.__stream || (group.node.__stream = { stableCount: 0, container: null, live: null, rawText: "" });
      const raw = stream.rawText + content;
      if (!window.emrgMarkdown.streamProject(body, raw, stream)) {
        body.textContent += content;
      }
      scrollToBottom(sid);
    }
  }

  /** 取消/错误收尾：移除该会话所有在途节点的 typing 光标（cancelled 事件无 request_id，只能全清） */
  function clearTyping(sid) {
    for (const group of st(sid).groupNodes.values()) {
      for (const node of group.nodes) {
        const body = node.querySelector(".msg-body") || node;
        body.classList.remove("typing");
      }
    }
  }

  /** done：整体 Markdown 渲染（requestIdleCallback 调度，G127）——该 rid 的全部文本段逐个渲染 */
  function handleDone(data, sid) {
    const { groupNodes, doneRids } = st(sid);
    const rid = data.request_id;
    if (rid) {
      doneRids.add(rid);
      if (doneRids.size > 500) doneRids.clear(); // UUID 不复用，超限即清防长期运行增长
      const group = groupNodes.get(rid);
      if (group) {
        for (const node of group.nodes) {
          const body = node.querySelector(".msg-body") || node;
          body.classList.remove("typing");
          // rant 21:00:28：流式投影结束 → live 块转 full 一次性校正（与旧 done 渲染同源 renderMarkdown）
          const stream = node.__stream;
          if (stream && stream.container) {
            const render = () => {
              window.emrgMarkdown.streamFinalize(body, stream.rawText).then(() => scrollToBottom(sid));
            };
            if (window.requestIdleCallback) {
              window.requestIdleCallback(render, { timeout: 2000 });
            } else {
              render();
            }
            continue;
          }
          // 非流式路径（既有）：✦ 标记是元素而非文本——流式时 body.textContent 含 "✦ " 前缀，
          // 直接整体 render 会让 "✦ # Title" 等块语法（标题/列表/代码围栏）解析失败
          // （前缀不在行首 → marked 不识别）。渲染前剥离前缀，渲染后重新插入标记保持视觉一致。
          const text = body.textContent.replace(/^✦\s*/, "");
          const render = () => {
            window.emrgMarkdown.renderMarkdown(text).then((html) => {
              body.innerHTML = html;
              body.insertBefore(el("span", { class: "msg-assistant-mark" }, "✦ "), body.firstChild);
              scrollToBottom(sid);
            });
          };
          if (window.requestIdleCallback) {
            window.requestIdleCallback(render, { timeout: 2000 });
          } else {
            render();
          }
        }
        groupNodes.delete(rid);
      }
    }
    if (data.timeout) {
      addSystemMessage(EMRG_Copy._t("chat.timeoutWarn"), sid);
    }
    // 工具调用次数上限中断（跨项目教训：截断的工作不提示 = 用户拿半成品）
    // 对齐 TUI：明确提示结果可能不完整 + 可继续（TUI 已有 "Try '继续' to resume"）
    if (data.content && /exceeded/i.test(data.content) && /max|limit|round/i.test(data.content)) {
      addSystemMessage(EMRG_Copy._t("chat.maxRoundsHint"), sid);
    }
  }

  /** rant 21:28:49：工具合并组 —— 组内工具行容器 + 状态 */
  function updateToolGroup(group, sid) {
    const rowsEl = group.querySelector(".tool-group-rows");
    const bar = group.querySelector(".tool-group-bar");
    if (!rowsEl || !bar) return;
    const rows = rowsEl.children.filter((c) => c.classList && c.classList.contains("tool-row"));
    const running = rows.some((r) => r.classList.contains("running"));
    const userExpanded = group.dataset.userExpanded === "1";
    if (rows.length >= 2 && !running) {
      // ≥2 行全部完成 → bar 显示 + 摘要（数量 + 总耗时）；非手动展开则收起
      const total = rows.reduce((acc, r) => acc + (parseFloat(r.dataset.elapsed) || 0), 0);
      const summary = bar.querySelector(".tool-group-summary");
      if (summary) {
        summary.textContent = EMRG_Copy._t("chat.toolGroupSummary", {
          count: rows.length,
          time: total.toFixed(1) + "s",
        });
      }
      bar.classList.remove("hidden");
      group.classList.toggle("collapsed", !userExpanded);
    } else {
      // 组内仅 1 行 或 存在 running 行 → bar 隐藏、rows 展开（进行中工具实时可见）
      bar.classList.add("hidden");
      group.classList.remove("collapsed");
    }
  }

  /** 工具友好状态行（进行中 → 完成/失败，默认折叠，点开展示原始输出） */
  function handleToolStart(data, sid) {
    const { groupNodes, toolRows } = st(sid);
    const rid = data.request_id;
    if (rid) {
      let group = groupNodes.get(rid);
      if (!group) {
        // G104：tool_start 也建组（LLM 先出 tool_calls 后出文本）
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn, sid);
        group = { node, nodes: [node], hasText: false, sealed: false };
        groupNodes.set(rid, group);
      } else if (group.hasText) {
        // rant 21:57:10：已有文本段之后来了工具 → 封存当前段，后续 delta 新建段（保持 TUI 交错顺序）
        group.sealed = true;
        // rant 21:09：已结束的文本段不再闪烁——封存时移除其 typing 光标，
        // 让 ▍ 只保留在最新一段文本后面（"前面的文本都结束了还显示闪烁光标"）。
        const prevBody = group.node.querySelector(".msg-body") || group.node;
        prevBody.classList.remove("typing");
      }
    }
    const phrases = EMRG_Copy.toolPhrases(data.tool_name);
    const row = el("div", { class: "tool-row running" });
    row.appendChild(el("span", { class: "tool-spinner" }));
    const label = el("span", { class: "tool-label" }, phrases.doing);
    row.appendChild(label);
    // Rant 2026-08-19T10:35:24: show the agent's intent (why this call
    // happened) as a human-readable purpose instead of raw argument JSON.
    if (data.intent) {
      row.appendChild(el("span", { class: "tool-intent" }, data.intent));
    }
    row.appendChild(el("span", { class: "tool-chevron" }, "⌄"));
    // 点展开 → 显示原始输出（等宽字体/圆角浅底）
    row.addEventListener("click", () => {
      const out = row.querySelector(".tool-output");
      if (out) {
        out.classList.toggle("hidden");
        row.classList.toggle("expanded", !out.classList.contains("hidden"));
      }
    });

    // rant 21:28:49：连续工具合并 —— 会话容器最后一个子节点判定
    // 1) 前一工具已完成（.tool-row:not(.running)）→ 新建 .tool-group，把旧行移入 rows，新行也入 rows
    // 2) 已是 .tool-group → 新行直接入其 rows（组收起时新工具 start 自动展开显示 running）
    // 3) 其他（文本/用户消息/无节点）→ 独立 .tool-row（现状不变；文本穿插不合并）
    const cv = chatContainer(sid);
    const last = cv.children.length ? cv.children[cv.children.length - 1] : null;
    let appendedToGroup = false;
    if (last && last.classList) {
      if (last.classList.contains("tool-row") && !last.classList.contains("running")) {
        const rowsEl = el("div", { class: "tool-group-rows" });
        const bar = el("div", { class: "tool-group-bar hidden" });
        bar.appendChild(el("span", { class: "tool-group-chev" }, "⌄"));
        bar.appendChild(el("span", { class: "tool-group-summary" }, ""));
        const grp = el("div", { class: "tool-group" });
        grp.appendChild(bar);
        grp.appendChild(rowsEl);
        // bar 点击 → 展开/收起 rows + chevron 旋转 + 组标 user-expanded（手动展开后不再自动收起）
        bar.addEventListener("click", () => {
          const wasCollapsed = grp.classList.contains("collapsed");
          if (wasCollapsed) {
            grp.classList.remove("collapsed");
            grp.dataset.userExpanded = "1";
          } else {
            grp.classList.add("collapsed");
            delete grp.dataset.userExpanded; // 手动收起后新工具完成仍自动收起（rule 5）
          }
        });
        // 移旧行入组（真实 DOM appendChild 即移动；mock 需先 remove 防残留）
        if (last.parentNode) last.remove();
        rowsEl.appendChild(last);
        rowsEl.appendChild(row);
        cv.appendChild(grp);
        appendedToGroup = true;
      } else if (last.classList.contains("tool-group")) {
        const rowsEl = last.querySelector(".tool-group-rows");
        if (rowsEl) {
          rowsEl.appendChild(row);
          last.classList.remove("collapsed"); // 组收起后新工具 start → 自动展开显示 running 行
          appendedToGroup = true;
        }
      }
    }
    if (appendedToGroup) {
      scrollToBottom(sid);
      App.updateEmptyState?.();
      const rowsEl = row.parentNode;
      if (rowsEl && rowsEl.classList.contains("tool-group-rows")) {
        updateToolGroup(rowsEl.parentNode, sid);
      }
    } else {
      append(row, sid);
    }
    toolRows.set(data.tool_call_id, row);
  }

  function handleToolEnd(data, sid) {
    const row = st(sid).toolRows.get(data.tool_call_id);
    if (!row) return;
    // rant 21:08：工具执行完成后 spinner 必须停止——移除转圈元素（CSS 亦有
    // .tool-row:not(.running) 隐藏兜底），只保留 ✓ 完成标记，防止"对号前面一直转圈"。
    row.querySelector(".tool-spinner")?.remove();
    const ok = !data.error;
    row.classList.remove("running");
    row.classList.add(ok ? "done" : "failed");
    const phrases = EMRG_Copy.toolPhrases(data.tool_name);
    const label = row.querySelector(".tool-label");
    if (label) {
      if (ok) {
        const check = el("span", { class: "tool-check" }, "✓ ");
        row.insertBefore(check, label);
        label.textContent = phrases.done;
      } else {
        label.textContent = EMRG_Copy.TOOL_FAIL_TEXT;
      }
    }
    const elapsed = data.elapsed !== undefined ? `${data.elapsed.toFixed(1)}s` : "";
    if (elapsed && ok) {
      row.title = EMRG_Copy._t("chat.elapsed", { s: elapsed });
      // rant 21:28:49：工具耗时可见 —— label 后 · 3.2s（失败不加）
      const time = el("span", { class: "tool-time" }, `· ${elapsed}`);
      if (label) label.after(time);
      row.dataset.elapsed = String(data.elapsed); // 供合并组摘要求和
    }
    // G91/G131：content 截断 2000 字符 + 展开全文
    const content = data.content || "";
    if (content) {
      const truncated = content.length > 2000 ? content.slice(0, 2000) + "…" : content;
      const out = el("div", { class: "tool-output hidden" }, truncated);
      row.appendChild(out);
      if (content.length > 2000) {
        const btn = el("button", { class: "tool-expand-btn" }, EMRG_Copy._t("chat.expand"));
        btn.addEventListener("click", () => {
          out.textContent = content;
          btn.remove();
        });
        row.appendChild(btn);
      }
    }
    scrollToBottom(sid);
    // rant 21:28:49：该行在合并组内 → 更新组摘要（数量 + 总耗时）与收起状态
    const rowsEl = row.parentNode;
    if (rowsEl && rowsEl.classList && rowsEl.classList.contains("tool-group-rows")) {
      updateToolGroup(rowsEl.parentNode, sid);
    }
  }

  return {
    addUserMessage,
    addSystemMessage,
    createAssistantNode,
    addHistoryMessage,
    setLoadBar,
    clear,
    scrollToBottom,
    handleDelta,
    handleDone,
    handleToolStart,
    handleToolEnd,
    clearTyping,
    registerContainer,
    unregisterContainer,
    hasContainer,
    chatContainer,
    // P3：会话级状态访问器（sid=null → 旧版单会话桶，兼容既有调用方）
    groupNodesFor: (sid) => st(sid).groupNodes,
    toolRowsFor: (sid) => st(sid).toolRows,
    get groupNodes() {
      return st(null).groupNodes;
    },
    get toolRows() {
      return st(null).toolRows;
    },
  };
})();

window.EMRG_Chat = Chat;
