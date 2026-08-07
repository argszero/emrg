"use strict";
/**
 * chat.js — 聊天渲染：用户气泡 / EMRG ✦ 消息 / 流式 delta / 工具友好行 / 系统消息。
 * 事件类型与 main 契约不变（message_delta/done/tool_started/tool_finished/…）。
 */

const Chat = (() => {
  // requestId → 消息 DOM 节点（广播分组）
  const groupNodes = new Map();
  // tool_call_id → 工具行 DOM 节点
  const toolRows = new Map();
  // rant 14:11：已 done 的 request_id（UUID 不复用）——残留 delta 直接丢弃，防孤儿节点
  const doneRids = new Set();

  /** 复制代码按钮（设计 §3.3）：事件委托在聊天区，CSP 无内联 handler */
  function initCodeCopy() {
    const cv = $("chat-view");
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

  /** 追加节点到聊天区并滚动 */
  function append(node) {
    $("chat-view").appendChild(node);
    scrollToBottom();
    // rant 14:11：任何消息增删都重新评估欢迎屏显隐（此前只在切会话时评估 → 首条消息后欢迎屏不隐藏）
    App.updateEmptyState?.();
  }

  function scrollToBottom() {
    const cv = $("chat-view");
    cv.scrollTop = cv.scrollHeight;
  }

  function clear() {
    $("chat-view").innerHTML = "";
    groupNodes.clear();
    toolRows.clear();
    doneRids.clear();
    App.updateEmptyState?.(); // rant 14:11：清空（切会话/新会话）也同步欢迎屏显隐
  }

  /** 用户消息：右对齐柔和气泡 */
  function addUserMessage(text) {
    const node = el("div", { class: "msg user" }, text);
    append(node);
    return node;
  }

  /** EMRG 消息：全宽 + ✦ 标识（流式节点，body 先 textContent 后整体 marked） */
  function createAssistantNode(isOwn) {
    const node = el("div", { class: "msg assistant" });
    if (!isOwn) {
      node.appendChild(el("div", { class: "remote-label" }, EMRG_Copy._t("chat.fromOtherClient")));
    }
    const mark = el("span", { class: "msg-assistant-mark" }, "✦ ");
    const body = el("div", { class: "msg-body typing" });
    body.appendChild(mark);
    node.appendChild(body);
    append(node);
    return node;
  }

  /** 系统消息（温和置中） */
  function addSystemMessage(text) {
    append(el("div", { class: "msg system" }, text));
  }

  /** 流式 delta（G122 main 已按 chunks 批量） */
  function handleDelta(chunks) {
    for (const chunk of chunks) {
      const rid = chunk.request_id;
      if (!rid || doneRids.has(rid)) continue; // rant 14:11：已 done 的流丢弃残留 delta，不建孤儿节点
      let group = groupNodes.get(rid);
      if (!group) {
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn);
        group = { node, nodes: [node], hasText: false, sealed: false };
        groupNodes.set(rid, group);
      } else if (group.sealed) {
        // rant 21:57:10：上一文本段被工具行"封存"→ 新文本段开新节点（旧节点保留在 DOM 原位）
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn);
        group.node = node;
        group.nodes.push(node);
        group.sealed = false;
      }
      const content = chunk.content || "";
      if (content) group.hasText = true;
      const body = group.node.querySelector(".msg-body") || group.node;
      // 流式中只动 textContent（不解析 Markdown）——性能约束
      body.textContent += content;
      scrollToBottom();
    }
  }

  /** 取消/错误收尾：移除所有在途节点的 typing 光标（cancelled 事件无 request_id，只能全清） */
  function clearTyping() {
    for (const group of groupNodes.values()) {
      for (const node of group.nodes) {
        const body = node.querySelector(".msg-body") || node;
        body.classList.remove("typing");
      }
    }
  }

  /** done：整体 Markdown 渲染（requestIdleCallback 调度，G127）——该 rid 的全部文本段逐个渲染 */
  function handleDone(data) {
    const rid = data.request_id;
    if (rid) {
      doneRids.add(rid);
      if (doneRids.size > 500) doneRids.clear(); // UUID 不复用，超限即清防长期运行增长
      const group = groupNodes.get(rid);
      if (group) {
        for (const node of group.nodes) {
          const body = node.querySelector(".msg-body") || node;
          body.classList.remove("typing");
          const text = body.textContent;
          const render = () => {
            window.emrgMarkdown.renderMarkdown(text).then((html) => {
              body.innerHTML = html;
              scrollToBottom();
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
      addSystemMessage(EMRG_Copy._t("chat.timeoutWarn"));
    }
    // 工具调用次数上限中断（跨项目教训：截断的工作不提示 = 用户拿半成品）
    // 对齐 TUI：明确提示结果可能不完整 + 可继续（TUI 已有 "Try '继续' to resume"）
    if (data.content && /exceeded/i.test(data.content) && /max|limit|round/i.test(data.content)) {
      addSystemMessage(EMRG_Copy._t("chat.maxRoundsHint"));
    }
  }

  /** 工具友好状态行（进行中 → 完成/失败，默认折叠，点开展示原始输出） */
  function handleToolStart(data) {
    const rid = data.request_id;
    if (rid) {
      let group = groupNodes.get(rid);
      if (!group) {
        // G104：tool_start 也建组（LLM 先出 tool_calls 后出文本）
        const isOwn = App.state.ownStreamRequestId === rid;
        const node = createAssistantNode(isOwn);
        group = { node, nodes: [node], hasText: false, sealed: false };
        groupNodes.set(rid, group);
      } else if (group.hasText) {
        // rant 21:57:10：已有文本段之后来了工具 → 封存当前段，后续 delta 新建段（保持 TUI 交错顺序）
        group.sealed = true;
      }
    }
    const phrases = EMRG_Copy.toolPhrases(data.tool_name);
    const row = el("div", { class: "tool-row running" });
    row.appendChild(el("span", { class: "tool-spinner" }));
    const label = el("span", { class: "tool-label" }, phrases.doing);
    row.appendChild(label);
    row.appendChild(el("span", { class: "tool-chevron" }, "⌄"));
    // 点展开 → 显示原始输出（等宽字体/圆角浅底）
    row.addEventListener("click", () => {
      const out = row.querySelector(".tool-output");
      if (out) {
        out.classList.toggle("hidden");
        row.classList.toggle("expanded", !out.classList.contains("hidden"));
      }
    });
    append(row);
    toolRows.set(data.tool_call_id, row);
  }

  function handleToolEnd(data) {
    const row = toolRows.get(data.tool_call_id);
    if (!row) return;
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
    scrollToBottom();
  }

  return {
    addUserMessage,
    addSystemMessage,
    createAssistantNode,
    clear,
    scrollToBottom,
    handleDelta,
    handleDone,
    handleToolStart,
    handleToolEnd,
    clearTyping,
    get groupNodes() {
      return groupNodes;
    },
    get toolRows() {
      return toolRows;
    },
  };
})();

window.EMRG_Chat = Chat;
