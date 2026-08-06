"use strict";
/**
 * result-panel.js — 结果面板（右栏 Artifacts，WorkBuddy P1，rant 21:35）
 *
 * 监听 tool_finished 事件，把工具输出/生成文件登记为产物卡片：
 *   - 工具输出（bash/read/write）→ 折叠卡片，可展开 + 复制
 *   - 文件路径（write 产物）→ 文件条目，点击用系统默认程序打开
 * 三栏布局：sidebar | chat | result-panel；⌘\ 折叠/展开；窄屏自动隐藏。
 */

const ResultPanel = (() => {
  const MAX_ITEMS = 50;

  function panel() {
    return $("result-panel");
  }
  function listEl() {
    return $("result-list");
  }

  function isCollapsed() {
    const p = panel();
    return p ? p.classList.contains("collapsed") : true;
  }

  function setCollapsed(collapsed) {
    const p = panel();
    if (!p) return;
    p.classList.toggle("collapsed", collapsed);
    try {
      localStorage.setItem("emrg.resultPanel.collapsed", collapsed ? "1" : "0");
    } catch { /* ignore */ }
  }

  function toggle() {
    setCollapsed(!isCollapsed());
  }

  /** 空状态 */
  function renderEmpty() {
    const list = listEl();
    if (!list) return;
    if (list.children.length === 0) {
      list.innerHTML = `<div class="result-empty">还没有产物，对话中生成的文件会出现在这里</div>`;
    }
  }

  /** 登记一个产物条目（tool_finished 事件） */
  function addToolResult(data) {
    const list = listEl();
    if (!list) return;
    // 空状态占位清除
    const empty = list.querySelector(".result-empty");
    if (empty) empty.remove();

    const card = el("div", { class: "result-item" });

    // 标题行：工具名 + 耗时
    const head = el("div", { class: "result-head" });
    const name = el("span", { class: "result-tool" }, data.tool_name || "tool");
    head.appendChild(name);
    if (data.elapsed !== undefined) {
      head.appendChild(el("span", { class: "result-elapsed" }, `${data.elapsed.toFixed(1)}s`));
    }
    head.appendChild(el("span", { class: "result-chevron" }, "⌄"));
    card.appendChild(head);

    const ok = !data.error;
    card.classList.add(ok ? "done" : "failed");

    // 尝试提取文件路径（write/bash 输出中的生成文件）
    const content = String(data.content || "");
    const filePath = extractFilePath(data.tool_name, content);

    if (filePath) {
      // 文件条目：点击打开
      const fileRow = el("div", { class: "result-file" });
      const namePart = el("span", { class: "result-filename" }, filePath.split(/[\\/]/).pop());
      fileRow.appendChild(namePart);
      fileRow.addEventListener("click", async () => {
        const res = await window.emrg.openFile({ filePath });
        if (res && res.ok === false) {
          // 打开失败提示（文件可能已删除）
          fileRow.title = res.error || "无法打开文件";
        }
      });
      card.appendChild(fileRow);
    }

    // 内容摘要（可展开）
    const preview = content.slice(0, 200);
    if (preview.trim()) {
      const body = el("div", { class: "result-body hidden" }, preview);
      card.appendChild(body);
      // 复制按钮
      const copyBtn = el("button", { class: "result-copy", type: "button" }, "复制");
      copyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        try {
          navigator.clipboard.writeText(content).then(() => {
            copyBtn.textContent = "✓ 已复制";
            setTimeout(() => { copyBtn.textContent = "复制"; }, 1500);
          });
        } catch { /* clipboard unavailable */ }
      });
      body.appendChild(copyBtn);
      // 点击头部展开/收起
      head.addEventListener("click", () => {
        body.classList.toggle("hidden");
        card.classList.toggle("expanded", !body.classList.contains("hidden"));
      });
    }

    // 裁剪到 MAX_ITEMS
    while (list.children.length >= MAX_ITEMS) {
      list.removeChild(list.firstChild);
    }
    list.appendChild(card);
  }

  /** 从工具输出提取生成的文件路径（write 工具 / bash 中的 >> 重定向 / 路径模式） */
  function extractFilePath(toolName, content) {
    if (!content) return "";
    const lines = content.split("\n");
    if (toolName === "write" || toolName === "edit") {
      // 常见格式：Wrote file: /path 或 /path/to/file.txt
      const m = content.match(/(?:Wrote|wrote|Written|created|已写入|写入)[^\n:：]*[:：]\s*([^\s\n]+)/);
      if (m) return cleanPath(m[1]);
      const fm = content.match(/([^\s\n]+\/[^\s\n]+\.(?:py|js|ts|md|txt|json|yml|yaml|toml|sh|css|html|png|jpg|svg|log))/);
      if (fm) return cleanPath(fm[1]);
    }
    if (toolName === "bash") {
      // bash 输出通常不带路径；带 "Created" 或重定向时尝试
      const m = content.match(/(?:Created|created|Generated|generated)[^\n:：]*[:：]\s*([^\s\n]+)/);
      if (m) return cleanPath(m[1]);
    }
    return "";
  }

  function cleanPath(p) {
    return String(p).replace(/[`"'，,。、;；]/g, "").trim();
  }

  function init() {
    const p = panel();
    if (!p) return;
    // 折叠按钮
    const btn = $("result-toggle");
    if (btn) btn.addEventListener("click", toggle);
    // 键盘 ⌘\ 或 Ctrl\
    document.addEventListener("keydown", (e) => {
      if (e.key === "\\" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        toggle();
      }
    });
    // 窄屏自动隐藏（<900px 折叠）
    const mq = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
    if (mq) {
      const onMq = (ev) => { if (ev.matches) setCollapsed(true); };
      if (mq.addEventListener) mq.addEventListener("change", onMq);
      if (mq.matches) setCollapsed(true);
    }
    // 恢复折叠状态
    try {
      if (localStorage.getItem("emrg.resultPanel.collapsed") === "1") setCollapsed(true);
    } catch { /* ignore */ }
    renderEmpty();
  }

  return { init, addToolResult, toggle, isCollapsed };
})();

// ⚠️ 必须暴露到 window：app.js 作为独立 <script> 加载，模块级 const 不跨 script 共享。
// 缺失会导致真实 GUI 打开即 ReferenceError（测试沙箱因共享 vm context 掩盖此问题）。
window.ResultPanel = ResultPanel;
