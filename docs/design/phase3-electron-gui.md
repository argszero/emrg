# Phase 3 设计：Electron GUI 客户端（emrg-gui）

> 主路线图：[`roadmap.md`](roadmap.md) Phase 3
> 关联文档：[`packaged-installer.md`](packaged-installer.md) §7.1（GUI 设计定稿）、[`phase2-daemon-manager.md`](phase2-daemon-manager.md)（协议参考实现，已实施 PR #320）
> 本文是 Phase 3 的完整设计：现状分析、技术栈、架构、代码结构、协议客户端、UI 设计、测试策略、验收标准。
> 技术栈：**Electron**（2026-08-03 用户决策，取代 PySide6，见项目记忆 `gui-stack-electron-decision.md`）
> 修订：v9 + 自动 review R1-R77（G1-G132 已采纳，见 §9 修订记录）；定稿阶段

---

## 1. 现状分析

### 1.1 已就绪的基础（全部保留）

| 基础 | 状态 | GUI 如何用 |
|------|------|-----------|
| WebSocket 协议（Phase 1） | ✅ PR #311-318 | 语言无关，Node `ws` 库直连 |
| 协议契约（token + auth_ok 首帧） | ✅ `docs/design/protocol-contract.md` | Node 照读 `~/.emrg/emrgd.port` + 发 auth |
| daemon_manager.py（Phase 2） | ✅ PR #320 | **协议参考实现**——Node 客户端照它写，行为一致 |
| 广播模型（多客户端同 session） | ✅ PR #318 | TUI + GUI 同开同 session 天然支持 |
| session 级锁（session busy） | ✅ | GUI 发 task 时收到 `{"error": "session busy"}` 需处理 |

### 1.2 为什么 Electron（2026-08-03 决策）

- **UI 能力最强**：HTML/CSS/JS 渲染 Markdown（marked）、代码高亮（highlight.js）、diff 有现成生态
- **协议语言无关**：WebSocket 是标准协议，Node 原生 `ws` 库直连 daemon，无需 Python 桥
- **打包成熟**：electron-builder 跨平台（.dmg/.exe/.AppImage）
- **代价（已接受）**：体积 ~80-200MB（含 Chromium）、内存 ~200-500MB/实例——GUI 是非开发者主入口，体验权重高

### 1.3 GUI 定位（与 TUI 的差异）

| 维度 | TUI | GUI |
|------|-----|-----|
| 目标用户 | 开发者（键盘驱动） | 非开发者（鼠标驱动，零学习成本） |
| 功能矩阵 | 完整（/命令、记忆浏览、skills） | v1 只做 80% 日常（聊天/会话/工具状态/设置） |
| 渲染 | 终端 Markdown | Chromium 富渲染（marked + highlight.js） |
| 状态 | 零状态只渲染 | 同——daemon 是唯一状态源 |

---

## 2. 架构总览

### 2.1 进程模型

```
┌─────────────────────────────────────────────────────────┐
│                     emrg-gui (Electron)                  │
│  ┌───────────────────┐          ┌─────────────────────┐  │
│  │  main 进程 (Node)  │  IPC     │  renderer (Chromium)│  │
│  │  ┌──────────────┐ │ ◄──────► │  ┌───────────────┐  │  │
│  │  │daemon_client │ │ preload  │  │ app.js (UI)   │  │  │
│  │  │  .js (ws)    │ │ 桥       │  │ markdown.js   │  │  │
│  │  └──────────────┘ │          │  │ settings.js   │  │  │
│  └───────────────────┘          └─────────────────────┘  │
└─────────────────────────────────────────────────────────┘
              ▲ ws://127.0.0.1:<port> + token
┌─────────────┴───────────────────────────────────────────┐
│                     emrgd (唯一 daemon)                   │
│      生命本体：LLM / 工具执行 / 演化 / 会话 / 记忆           │
└─────────────────────────────────────────────────────────┘
```

**核心原则**：
1. **main 进程是唯一连 daemon 的进程**——`daemon_client.js` 在 main 内，renderer 零网络权限（安全沙箱）
2. **renderer 只渲染**——所有 daemon 交互经 preload IPC 桥转发 main
3. **不内嵌 daemon**——main 启动时检查/拉起 `emrgd`（spawn `python -m emrg.server`），复用 TUI 的 daemon 生命周期逻辑
4. **窗口关闭 ≠ daemon 退出（G40）**：GUI 关窗只断开 ws（daemon 是独立进程继续运行，同 TUI 退出语义，架构原则"服务端不随客户端退出而终止"）。main 的 `window-all-closed` 按 Electron 惯例：macOS 不退出（dock 再激活），其它平台 `app.quit()`。退出前 `daemon_client.close()` 优雅断连
5. **单实例锁（G85+G120）**：main 用 `app.requestSingleInstanceLock()`——第二个 GUI 实例启动时直接退出并 focus 已有窗口。理由：① 两个 GUI 连同一 daemon、可同时操作**同一会话**（会话锁只挡并发 task，不挡 UI 状态竞争）；② **daemon spawn 竞争**——两实例同时判定 port 文件缺失 → 双 spawn。**但注意（G120）**：daemon 自身已有 pid 文件原子互斥（daemon.py:130-141 O_CREAT|O_EXCL，已运行则退出）——**双 spawn 不会起两个 daemon**（第二个自我退出），G85 ② 的实际风险是**双客户端各自连上后 UI 竞争**（①）而非 daemon 重复。GUI+TUI 同开时同理：TUI 已拉起 daemon，GUI 的 ensureConnected 读 port 文件直连即可，**无竞态**（daemon 互斥兜底）。
6. **菜单与 DevTools（G86）**：**保留默认菜单的编辑项**（macOS Cmd+C/V/X/A 依赖「Edit」菜单，`Menu.setApplicationMenu(null)` 会废掉剪贴板快捷键）；生产（app.isPackaged）禁 DevTools（`webContents.openDevTools` 不响应 + `F12`/`Cmd+Alt+I` 不绑定）；开发模式可开。菜单可精简为：应用（macOS）/ 编辑 / 视图（重载/DevTools 开发时）/ 窗口
7. **窗口状态（G87）**：v1 记住窗口 bounds（`win.getBounds()` 存 `~/.emrg/gui-window.json`，启动时 `setBounds` 恢复）——非必需但成本极低；不持久化则每次居中默认 1200×800
8. **renderer 崩溃恢复（G101）**：监听 `render-process-gone`（renderer 崩溃/被系统杀）→ 显示"界面已崩溃，正在恢复…" → 重新 `loadFile`（renderer 无状态，main 重新走 init 流程即可——窗口对象、daemon_client、IPC handler 都在 main 不受影响）。`unresponsive`（卡死）→ 提示可"重新加载"或等待。
9. **IPC 输入校验（G102+G114）**：main 对 renderer 传入的 IPC 参数做基本校验（纵深防御——CSP/沙箱防了 XSS，但渲染层被攻破后仍能调 IPC）：`sessionId` 必须匹配 `/^s_\d{6}_\d{4}_[0-9a-f]{4,8}$/`；`text` 长度 ≤ 20000 字符；`config` 只接受 **`{apiKey, baseUrl, model, projectDir}` 四键白名单**（G114：G112 后 SettingsDialog 有 4 字段——原三键白名单会剥掉 projectDir，保存丢目录；剥掉多余键）；非法参数 → reject + 记日志，不 panic

### 2.2 cwd 来源（G6，架构级）

TUI 用 `os.getcwd()`（终端启动目录）决定 session_id 生成、项目跟踪、任务执行目录。**GUI 无终端 cwd 概念**——v1 设计：

| 方案 | 说明 |
|------|------|
| **A. 首启选项目目录**（v1 主方案） | 首启引导时弹目录选择器，存入 `~/.emrg/config.toml` 的 `[gui] project_dir`；所有操作（newSession/sendTask/listSessions）用它做 cwd |
| **B. 默认兜底** | 用户跳过选择时默认 `~/.emrg/evolution`（演化项目） |
| C. 会话级多目录 | 每个会话关联不同目录（v2+，不在本文） |

main 进程读 config 的 `project_dir` → 作为 `DaemonClient` 的默认 cwd → renderer 经 `emrg:init` 拿到并显示（状态栏可显示项目名）。

### 2.3 目录结构

```
emrg/gui/
├── package.json          # Electron 入口 + 依赖 + scripts（G53/G54）
│   #   runtime deps: ws / smol-toml / marked / highlight.js / dompurify（G44 消毒）
│   #   devDeps: electron / electron-builder / vitest 或 node:test（mock ws 单测）
│   #   scripts: start（electron .）/ test（单测）/ test:integration / e2e（Playwright for Electron）/ dist（electron-builder）
├── main.js               # main 进程：窗口创建、daemon 生命周期、daemon_client 管理
├── preload.js            # contextBridge：renderer ↔ main 安全 IPC 桥
├── daemon_client.js      # Node ws 客户端（协议参考 daemon_manager.py）
├── renderer/
│   ├── index.html        # 主布局：会话栏 + 聊天区 + 输入条 + 状态栏
│   ├── app.js            # UI 逻辑：消息渲染、会话列表、工具状态、事件绑定
│   ├── markdown.js       # marked + highlight.js 封装（流式增量渲染）
│   ├── settings.js       # 设置对话框（config.toml 读写，经 preload IPC）
│   └── styles.css        # 样式（暗色主题）
└── assets/               # 图标（icns/ico/png）——见 G39：统一引用 packaging/assets/，不重复维护
```

---

## 3. daemon_client.js（核心，协议参考 daemon_manager.py）

### 3.1 职责与接口

```javascript
// daemon_client.js — main 进程内唯一与 daemon 通信的模块
// 协议语义完全对照 emrg/client/daemon_manager.py（Phase 2 参考实现）

class DaemonClient {
  // ── daemon 生命周期（对照 daemon_manager.is_running/start_daemon/ensure_connected）──
  async ensureConnected()      // 读 port 文件 → ws 连接 → auth 首帧 → auth_ok；未运行则拉起
  async startDaemon()          // spawn python -m emrg.server（sys.frozen 分支：同目录二进制）
  isRunning()                  // TCP 探测（对照 is_server_running_sync）——⚠️ G43：不可简化为"port 文件存在"
                               //   （stale port 文件 ≠ daemon 存活；漏判死 daemon 会导致永不拉起）

  // ── 消息发送（对照 DaemonConnection.send_task/send_command）──
  sendTask({ sessionId, cwd, prompt, stream = true, images = null })
  // ⚠️ 内部生成 request_id（uuid）并缓存为"当前发起流"（G7）——
  // 收到 done 帧时比对 request_id 清除；非匹配的 delta/done 帧 = 广播流（他人发起，G3）
  // ⚠️ 缓存清理时机（G124）：除 done 外，**G94 超时兜底（30s 无 done）、G89/G119 断连、
  //    下一次 sendTask 覆盖**都必须清"当前发起流"缓存——残留会导致：① 旧 request_id 仍匹配
  //    后续广播帧（误判自有流）；② pending 表（G93）残留旧条目。清理统一走 `clearActiveStream()`
  //    （清缓存 + 分组 + 超时 timer），sendTask 前置调用。
  // ⚠️ request_id 必须作为 task 帧的 id 字段发出（G32）——daemon 只回显 req.id 不自生成
  //    （daemon.py:1074 `"request_id": req.id`；TaskRequest.id 默认 uuid4）。若不发 id，
  //    daemon 侧生成默认 uuid → 回显的 request_id 与本地缓存不匹配 → 流式无法结束/自有流与广播流混淆
  // ⚠️ stream 必须显式放进 payload（G96）——daemon 读 data.get("stream", False)（daemon.py:326），
  //    漏发 stream:true → 走非流式路径（_run_chat_once）→ GUI 收不到任何 delta、只有 done 帧，
  //    且 G7 的"流式分组/节流"全部失效。payload 恒含 stream:true（对照 TaskRequest.to_dict，
  //    protocol.py:35-47：type/id/session_id/cwd/prompt/timestamp/stream 全量字段）
  // ⚠️ images 为 v2 预留（G18）——v1 恒 null（无图片 UI）；单测覆盖 images 非空时 payload 正确
  sendCommand(type, params = {})   // ping/list_*/set_*/rant/... 只发不读

  // ── 事件接收（对照 read_stream）──
  onEvent(callback)            // 注册帧回调：delta / tool_start / tool_end / done / error
  close()                      // 关闭 ws
}
```

### 3.2 连接流程（对照 connect.py:49-75）

```javascript
// ⚠️ Node main 进程用 require('ws')（G10）——Electron 内置 Node 无原生 WebSocket，
// ws 库 API 兼容浏览器（onmessage/onclose/onerror），但需显式引入
// ⚠️ maxPayload（G62+G105 修正）：new WebSocket(url, { maxPayload: 16 * 1024 * 1024 })
//   ⚠️ G105 更正：bash 工具输出有 MAX_OUTPUT_CHARS=200_000 截断（bash_tool.py:16，200KB），
//   read/grep 同理有上限（read_tool.py 256KB / grep_tool.py 200 条）——**tool_end 帧不会超 16MB**。
//   原 G105"放宽 64MB"是过度设计（误判工具输出可达数 MB）——维持 16MB（对齐 Python max_size=16MB，
//   connect.py:65，双向一致）。G91 的 2000 字符截断显示仍保留（200KB 渲染也卡）。
const WebSocket = require('ws');

// startDaemon 的 Python 路径（G16+G61）：
// - 源码运行（npm start）：spawn 项目 .venv/bin/python -m emrg.server（G59：项目根 = path.resolve(__dirname, '../..')，
//   即 emrg/gui/ 上溯两级；无 EMRG_ROOT env——TUI 用 __file__ 定位，Node 用 __dirname）
//   ⚠️ G61 跨平台：.venv/bin/python 仅 macOS/Linux；Windows 为 .venv\Scripts\python.exe（path.join 处理）
//   兜底：.venv 缺失时依次尝试 PATH 上的 python3 / python（找不到则报错提示创建 .venv）
// - 打包后（app.isPackaged）：spawn process.resourcesPath/emrgd（PyInstaller 产物，见 G17/G38）
//   ⚠️ G68 spawn 选项：{ stdio: 'ignore', detached: true } + child.unref()——
//     对照 Python start_daemon 的 DEVNULL + start_new_session=True（daemon 脱离 GUI 进程组，
//     GUI 退出不带走 daemon，架构原则"服务端不随客户端退出而终止"）；stdio ignore 防 GUI 持有管道阻塞
//   ⚠️ G125 spawn 设置 cwd=project_dir：daemon 启动时 load_skills() 用 Path.cwd()（daemon.py:120）
//     加载【项目级】skills（project_dir/.emrg/skills/）——GUI 不设 cwd 则 daemon 继承 GUI 启动目录
//     （可能是 / 或主目录）→ 项目 skills 加载不到。对照 TUI：用户在项目目录启动，daemon cwd 自然正确；
//     GUI 从任意目录启动（app 双击）必须显式设 cwd=project_dir（从 config 读，G6）恢复同语义。
//     首启时 daemon 是保存 config 后才拉起（G123）——此时 project_dir 已定，可安全传 cwd。
async function ensureConnected() {   // G31：独立函数需 function 关键字
  // 1. 读 ~/.emrg/emrgd.port → { port, token }（两行：port\n token）
  // 2. 若 port 文件不存在 → startDaemon() 拉起（等最多 5s）
  // 3. new WebSocket(`ws://127.0.0.1:${port}`)
  // 4. 首帧发 {"type": "auth", "token": token}
  // 5. 等 auth_ok（10s 超时）→ 就绪；否则报错
  // 6. 注册 message 监听 → JSON.parse → 分发到事件队列
  // ⚠️ G43（stale port 文件）：ws 连接失败/超时（ECONNREFUSED/ETIMEDOUT）——
  //    即使 port 文件存在，也须删 ~/.emrg/emrgd.port → startDaemon() 拉起 → 重试连接。
  //    对照 Python ensure_connected：is_running（TCP 探测）为 false → cleanup_server + start_daemon
}
```

**⚠️ 关键语义（对照 daemon_manager）**：
- **坏 JSON 帧**：忽略（log warning），不崩——同 daemon_manager.recv（R53）
- **断连（ws close/error）**：触发重连回调（同 ConnectionClosed 传播语义，R11）——状态栏变红 + 自动重连
- **session busy**：daemon 会回 `{"error": "session busy"}`（session 级锁）——UI 提示"该会话正忙"
- **ping 时机（G19）**：`ensureConnected()` 成功**后发一次 ping**（拿 server_id/model/evolution_count 填充状态栏），重连成功后同样发一次。**不做定期轮询**（TUI 也不轮询，演化计数是快照）
- **server_id 格式（G108）**：对照 TUI app.py:326-328——`{instance_id[:8]} @ {host_name}`（pong identity 含 instance_id/host_name/fork_source/branch_id，daemon.py:616-617）；状态栏显示 `server_id + [model]`（如 `3f2a9b1c @ MacBook-Pro [deepseek-chat]`）。model 字段直接读 pong 的 `model`（daemon.py:624，无需 list_models）。**窗口标题同步（G109）**：TUI 切会话同步终端标题（app.py setTitle 同款）——GUI 窗口标题 v1 固定 `EMRG`，**切会话时可加 ` — {会话显示名}`**（title 或 session_id 兜底，G27）；grow 到 OS 任务栏可辨识，成本极低（`win.setTitle`）。

**session_id 生成（G14，对照 emrg/session.py:33）**：
```javascript
// daemon 无 new_session 消息——session_id 由客户端本地生成（对照 Python generate_session_id，session.py:33-46）
function generateSessionId(cwd) {
  // 格式 s_YYMMDD_HHMM_xxxx，查重 cwd/.emrg/sessions/
  const now = new Date();
  const yymmdd = String(now.getFullYear()).slice(2) + pad(now.getMonth()+1) + pad(now.getDate());
  const hhmm = pad(now.getHours()) + pad(now.getMinutes());
  for (let i = 0; i < 100; i++) {
    const suffix = randomHex(4);  // 4 hex chars（对照 secrets.token_hex(2)[:4]）
    const sid = `s_${yymmdd}_${hhmm}_${suffix}`;
    if (!fs.existsSync(path.join(cwd, '.emrg', 'sessions', sid))) return sid;
  }
  // G81：100 次碰撞兜底——返回 8 hex 长后缀（对照 Python session.py:45-46 fallback）
  // 原实现循环外无返回 → undefined 会流到 sendTask payload（session_id 缺失 → daemon 报 task requires session_id）
  return `s_${yymmdd}_${hhmm}_${randomHex(8)}`;
}
// daemon 侧 _get_or_create_session 惰性创建——GUI 直接用它发消息即可
```

### 3.3 事件流（对照 daemon read_stream + TUI read_server 分类）

**⚠️ 帧分类逻辑（G1）**：服务器帧**不都有 `type` 字段**——`TaskResponse` 帧（delta/done）靠 `delta`/`done`/`request_id` 标志区分。Node 端分类：

```javascript
// daemon_client.js 事件分类（对照 TUI read_server 的 TaskResponse.from_dict 语义）
function classify(frame) {
  if (frame.type === 'tool_start')   return { event: 'tool_started',  data: frame };
  if (frame.type === 'tool_end')     return { event: 'tool_finished', data: frame };
  if (frame.type === 'cancelled')    return { event: 'cancelled',     data: frame };  // G4
  if (frame.type === 'sessions_list' || frame.type === 'models_list' ||
      frame.type === 'history_list'  || frame.type === 'tasks_list') {
    return { event: 'list_result', data: frame };                                   // G4
  }
  if (frame.type === 'resume_result' || frame.type === 'model_set' ||
      frame.type === 'session_deleted' || frame.type === 'clear_result') {
    return { event: 'command_result', data: frame };  // G57：命令结果帧（原落 unknown，switchSession meta 拿不到）
  }
  if (frame.done)                    return { event: 'done', data: frame };          // G2：三种形态
  if (frame.delta)                   return { event: 'message_delta', data: frame };
  if (frame.error)                   return { event: 'error', data: frame };
  if (frame.uptime_seconds !== undefined) return { event: 'pong', data: frame };
  return { event: 'unknown', data: frame };
}
```

**done 帧三种形态（G2）**：
| 形态 | 帧结构 | UI 处理 |
|------|--------|---------|
| 正常 | `{request_id, content: 全文, done: true, delta: false, session_id}` | 结束流式 + 恢复输入条 |
| LLM 错误 | `{done: true, request_id}`（无 content，先发了 error 帧） | 结束流式 + 恢复输入条；**不得清空已显示内容**（error 帧已提示） |
| 取消 | `{request_id, content: "", done: true, cancelled: true}` | 结束流式 + 提示"已中断" |

> **G63（done 帧字段）**：正常 done 帧**带 `delta: false` 和 `session_id`**（daemon.py:1469-1474）——classify 先查 `frame.done` 再查 `frame.delta`（顺序正确，false 不会误入 delta 分支）；session_id 可用于 debug/过滤。取消 done 帧**无 delta 字段**（daemon.py:1163-1168：`{request_id, content:"", done:true, cancelled:true, session_id}`）——classify 同样先命中 done，无歧义。G4 取消形态实为「cancelled 帧后跟 done 帧」：daemon cancel 处理回 `{"type": "cancelled", session_id}`（daemon.py:292）→ 任务协程被取消 → `_run_tool_loop_locked` 广播带 `cancelled: true` 的 done 帧。

> **G64（auth_ok 消费）**：`{"type": "auth_ok"}` 帧由 `ensureConnected()` 第 5 步**消费**（对齐 connect.py:71-77 的 wait_for auth_ok）——**不进入 onEvent 分发**；classify() 无需（也不应）识别 auth_ok，若事件流中出现说明连接流程实现有误。

**⚠️ 广播场景（G3）**：同 session 的所有订阅者收到**所有帧**（包括其他客户端发起的 task）。GUI 必须：
1. 区分"自己发起的流"（`request_id` 匹配当前发送）vs "广播来的流"（他人发起）
2. 广播来的 delta 按 `request_id` 分组——新 request_id 建新消息节点并标注"（来自其他客户端）"，同 request_id 追加到同一节点
3. GUI 自己不在 busy 状态也收到广播 delta——UI 需支持"被动接收流"

**分组生命周期（G83+G104）**：每个 request_id 分组的状态（节点引用 + 消息缓冲）在**流结束时清理**——done（三形态，G2）/ cancelled 帧到达即删除该分组 + 标记消息完成（「来自其他客户端」标注保留在消息节点上，**分组缓存删除**）。防泄漏护栏：
- **超时兜底**：分组创建后 10 分钟无任何帧 → 强制删除（陈旧广播残留——TUI 侧任务可能已死但 GUI 未收到 done）
- **上限保护**：同时活跃分组 > 20 → 丢弃最老分组（拒绝服务保护，广播风暴场景）

> **G104（分组在 tool_start 时也创建）**：LLM 先出 tool_calls 再出文本（工具调用在前）——`tool_start` **可能先于首个 delta 到达**。因此**新 request_id 的 tool_start 同样触发建分组**（不只在 delta 时，G3）。工具卡片挂在**该分组的消息节点内**（tool_start/tool_end 都带 `request_id`，daemon.py:1383-1388/1438-1443）：自有流 → 当前 assistant 节点；广播流 → 「来自其他客户端」分组节点。tool_end 匹配 tool_call_id 更新卡片（G20）不受分组清理影响（卡片已完成、分组缓存删）。**无 request_id 的 tool 帧**（理论上 daemon 总会带）→ 挂到当前分组或丢弃 + log warning。

| 服务器帧 | 事件 | UI 动作 |
|---------|------|---------|
| `uptime_seconds`（ping 响应，无 type） | `pong` | 状态栏更新（server_id/model/evolution_count） |
| `tool_start`（有 type） | `tool_started` | 工具状态行「🔧 bash — 运行中…」——**记住 tool_call_id（G20）** |
| `tool_end`（有 type） | `tool_finished` | **按 tool_call_id 匹配**更新卡片（G20）：完成/失败（error:true 红色）+ 输出折叠 |
| delta 帧（无 type，`delta:true`） | `message_delta` | 聊天区增量追加（按 request_id 分组） |
| done 帧（无 type，`done:true`） | `done` | 结束流式；恢复输入条（三形态见上） |
| `{"type": "cancelled"}` | `cancelled` | 结束流式；提示"已中断" |
| `resume_result`/`model_set`/`session_deleted`/`clear_result`（有 type） | `command_result`（G57） | switchSession 的 meta（message_count/title）填充占位；**model_set 是广播帧（daemon.py:409 发全部连接）→ 状态栏 model 名自动同步**（TUI 切模型 GUI 跟着变）；删除/清空结果提示 |
| `{"error": ...}` | `error` | 错误提示。**两类区分（G42）**：① session busy（`{"error": "session busy"}`，无流式）→ 立即恢复输入条 + 提示"该会话正忙"；② 流中错误（先 error 后 done）→ 提示但不结束流式（等 done 帧）。**session busy 帧带 session_id（G128）**：daemon.py:315 的 busy error 含 `session_id` 字段——UI 可据此**定位到具体会话**显示「会话 X 正忙（其他客户端/任务占用中）」；多会话场景下不误导用户（若当前打开的就是该会话 → 输入条禁用提示；若是其他会话 → 提示"该会话正忙"且不打断当前操作） |

**流结束兜底（G94）**：daemon 的 LLM 错误路径会发 done（daemon.py:1272-1282），但**wrapper 层未捕获异常 / daemon 进程崩溃**时**无 done 帧**（_run_tool_loop_locked 只释放锁不发 done，daemon.py:1480+）→ 自有流会永久"生成中"。护栏：**最后一个帧（delta/error/tool_end）后 30s 无 done** → 强制结束流式 + 提示"响应超时（连接可能已中断）"；广播流同（可复用 G83 超时兜底，广播用 10 分钟、自有流用 30s）。

**协议健壮性（G95）**：
- **pong 超时**：init 序列的 ping 发出后 5s 无 pong → 视为连接异常 → 走 §3.4 重连（对照 Python check_and_restart 的 3s ping 超时，daemon_manager.py:105）
- **旧 daemon 兼容**：daemon 对未知消息回 `{"error": "unknown message type", "received": ...}`（daemon.py:975-979）——若 GUI 用了新消息而 daemon 是旧版：error 帧经 G93 的 pending 表 reject → UI 提示"daemon 版本过旧，请更新"。**v1 只提示，不做自动降级**（GUI 与 daemon 同包发布，Phase 4 起版本同步）
- **unknown 帧**：classify 落 unknown → main log warning + 丢弃（不崩，不打扰 renderer）
| `sessions_list`/`models_list`/`history_list`/`tasks_list` | `list_result` | 会话/模型/任务列表填充 |

> **tool_call_id 关联（G20）**：tool_start 建卡片时存 `tool_call_id` → tool_end 按它匹配更新（对照 TUI app.py:475-510）。tool_end 的 `error: true` 表示工具失败，卡片显示红色。tool_start 的 `arguments` 用于显示命令（如 bash 的 command 字段）。

> **cancel 语义（G33）**：daemon 的 `_tool_task` 是**每连接**状态（daemon.py:229，连接协程内局部变量）——`cancel` 只取消**本连接发起**的任务（daemon.py:283-297）。GUI 在广播流（他人发起）上按 ⏹ 无效（daemon 仍回 `cancelled` 帧但实际任务继续跑）。**UI 应只在"自己发起流"（request_id 匹配）时显示 ⏹**（G3 的区分逻辑可复用）。`cancelled` 帧只发给 cancel 发起连接（`self._send(ws, ...)`，不广播给 session 订阅者）——但被取消任务的 done 帧（`cancelled: true` 形态）照常广播。

### 3.4 断连重连（对照 TUI _reconnect）

```
ws close/error → 状态栏变红（"daemon 连接断开"）
→ 状态栏显示"重连中…"（对齐 TUI "reconnecting..."，app.py:296）
→ 每 1s 尝试 ensureConnected()
→ 成功 → 状态栏变绿 + 重新 list_sessions + **重新 resume 当前打开的会话**（G41）
→ 若 daemon 进程也死了 → startDaemon() 拉起 → 重连
```

> **G41（重连恢复会话订阅）**：daemon 的广播订阅是**连接级**（`_session_subscribers` 按 ws 对象，daemon.py:270-278）——重连 = 新 ws = 订阅全丢。重连成功后除 list_sessions 外，main 必须重新 `resume_session` **当前打开的 session_id**（恢复订阅 + 刷新 meta.message_count），否则该会话的新消息/广播收不到。

> **G88（auth 失败 ≠ 瞬断，停止自动重试）**：对照 connect.py:36-42 `AuthError`——「token 不匹配/daemon 版本不匹配是配置问题，重连也修不好」。ensureConnected 第 5 步**收到 auth_ok 前 ws 就 close**（daemon 拒绝）→ 判定 auth 失败：**停止 1s 重试循环**，状态栏显示"认证失败（请检查 ~/.emrg/emrgd.port 或重启 daemon）" + 仅用户手动"重试"才再连。区分依据：**连接成功但 auth 被拒**（收到 close 无 auth_ok）vs **连接失败**（ECONNREFUSED 等，继续重试）。对照 Python：AuthError 抛出、非 ConnectionRefusedError。

> **G129（无协议版本协商——现状确认）**：`auth_ok` 帧是纯 `{"type": "auth_ok"}`（daemon.py:250），**不含版本号**——GUI 无法在 auth 时做版本协商。协议兼容完全依赖 G95 兜底（未知消息 → unknown message type → pending reject + 版本提示）。**v1 接受此现状**（GUI 与 daemon 同包发布，Phase 4 同步版本；协议稳定、帧结构多年未变）。**不实现**"auth 带版本"的 daemon 改动（违反"零改动 Python 核心"原则，G5 同源）。

> **G89（断连时进行中流的处理）**：对照 TUI `busy = False  # pending request is lost`（app.py:295）——断连时正在流式的消息（自有流 + 广播流）**标记「（连接中断）」**，流式状态清除，输入条恢复。**v1 不自动恢复**（G12 无历史加载，重连后无法重建内容）——用户看到中断标记后可自行重发；重连成功不补发。

> **G97（断连时工具卡片与分组清理）**：tool_start 已建卡片、tool_end 未到（断连丢失）→ 卡片**标记「（结果未知——连接中断）」**，不再显示"运行中"旋转态。同步清理：G83 广播分组 + G93 pending 命令队列 + 自有流缓存**全部清空**——重连后状态干净，避免"幽灵"卡片/陈旧 pending 与重连后 list_sessions/resume（G41）拿到的真实状态混淆。工具卡片无"重试"语义（工具已在 daemon 侧执行完毕/失败，重发会重复执行副作用）——只标记，不自动重跑。

---

## 4. IPC 桥（preload.js + main）

### 4.1 安全模型

- `contextIsolation: true` + `nodeIntegration: false`（默认安全）
- `preload.js` 用 `contextBridge.exposeInMainWorld('emrg', api)` 暴露白名单 API
- renderer **无法**直接访问 fs/网络——所有操作经 IPC
- **CSP（G26）**：`index.html` 加 `Content-Security-Policy` 响应头（`default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`）——marked/highlight.js 以本地文件加载（非 CDN），禁止外联；防 XSS 注入（聊天内容渲染 Markdown 时必须消毒——**用 DOMPurify（G44）**；marked 的 `sanitize` 选项已在 v8 移除，勿写）
- **sandbox（G45）**：webPreferences 加 `sandbox: true`——preload 只 import ipcRenderer/contextBridge（contextIsolation 已开），不需要 Node API
- **外部导航（G46）**：`setWindowOpenHandler` 一律 deny（禁 window.open/新窗口）；Markdown 渲染出的链接点击默认不导航，或经白名单协议（http/https）`shell.openExternal` 交给系统浏览器
- **config 路径（G26）**：main 进程读 `~/.emrg/emrgd.port` 和 `~/.emrg/config.toml`（对照 `emrg/config.py` `config_dir()` = `Path.home()/'.emrg'`）；Node 用 `os.homedir()`

### 4.2 IPC 通道清单

| 通道 | 方向 | 参数 | 返回 |
|------|------|------|------|
| `emrg:init` | renderer→main | — | 初始状态（server_id/model/会话列表/**api_key_configured**/**config_exists**/**project_dir**）。**内部序列（G34+G71）**：① 检查 config.toml 存在性（**缺失 → 不拉起 daemon，直接返回缺配置状态**——daemon 在 config 缺失时启动即崩，__main__.py:29）② ensureConnected ③ ping（拿 model/演化计数）④ list_sessions(project_dir)——renderer 一次性拿到三件套 + 首启引导判定（G36）**+ project_dir（G112，G6 明确"renderer 经 emrg:init 拿到并显示"但此前返回列表遗漏）** |
| `emrg:sendMessage` | renderer→main | {sessionId, text} | 请求已接受。**⚠️ cwd 由 main 从 config.project_dir 注入（G22）**——renderer 不传 |
| `emrg:listSessions` | renderer→main | — | 会话列表。**cwd 由 main 注入 project_dir**（daemon 校验 list_sessions requires cwd，daemon.py:710） |
| `emrg:switchSession` | renderer→main | {sessionId} | resume 结果 + **meta（消息数）**——历史不通过 resume 返回（G13，见 §5.4）。**⚠️ 订阅切换副作用（G66）**：daemon 的广播订阅按「消息携带的 session_id」切换（daemon.py:270-278）——main 必须发 `resume_session` 才能把本连接的订阅从旧会话移到新会话；只 list_sessions 不 resume 则新会话消息/广播收不到。**⚠️ 流式进行中切换策略（G65）**：见 §5.4 下方 |
| `emrg:deleteSession` | renderer→main | {sessionId} | 删除结果 |
| `emrg:newSession` | renderer→main | — | 新 session_id。**⚠️ cwd 由 main 注入 project_dir（G112，同 G22 语义）**——renderer 不传（原表 `{cwd}` 与 G22/preload 无参矛盾，修正）。**⚠️ 本地生成（G14）**——daemon 无 new_session 消息，session_id 由客户端生成（对照 `emrg/session.py` `generate_session_id`：`s_YYMMDD_HHMM_xxxx` + 查重 `cwd/.emrg/sessions/`）；daemon 侧 `_get_or_create_session` 惰性创建 |
| `emrg:setModel` | renderer→main | {model} | model_set 结果 |
| `emrg:listModels` | renderer→main | — | 模型列表（**G25**——设置对话框显示可用模型用；daemon 返回 models_list） |
| `emrg:saveSettings` | renderer→main | {config} | 写 config.toml 结果。**⚠️ 保存后 daemon 可能重启（mtime 检测，G11）**——GUI 走 §3.4 重连流程 + 状态栏提示"配置已保存，daemon 重启中…"。**⚠️ 重启期间状态（G119）**：保存后 daemon 约 1-3s 内自杀重启（check_and_restart 流程）——期间 ws 会 close（G88 重连逻辑触发）；**pending 命令全 reject（G93 规则 4）**；**流式中的任务被 daemon 自杀打断**（daemon 重启 = 任务丢失，同 G89 断连语义）→ 消息标「连接中断」。**UI 提示层级（G119）**：状态栏「配置已保存，daemon 重启中…」→ 重连成功 → 状态栏恢复 + **list_models 刷 model（G98）** + list_sessions + resume 当前会话（G41）。**⚠️ 避免"保存→立即重连但 daemon 还在自杀"竞态**：保存后不主动调 ensureConnected，等 ws close 事件自然触发重连循环（G88）——主动重连会连上"正在关闭"的旧 daemon。**⚠️ 重连完成后刷新模型显示（G98）**：daemon 重启后 model 可能已变（新配置生效）——重连成功（G41 的 list_sessions/resume 之外）补一次 `list_models`（拿新 `current`）→ 状态栏 model 更新；否则状态栏显示旧 model（TUI 保存配置也有同样行为，重启后模型名跟随新配置）。**⚠️ 全量读-改-写（G60）**：用 smol-toml 解析现有文件 → 只改 api_key/base_url/model 字段 → 全量序列化写回——**保留 [llm] 全部现有键**（max_tokens/temperature/context_window/vision/models/...）与其它未知段（如 [gui]）；只写三个字段会丢配置。**⚠️ 写入权限 0o600（G69）**：config.toml 含明文 api_key，fs.writeFile mode 0o600（对照 emrgd.port 的 0o600） |
| `emrg:getSettings` | renderer→main | — | 当前 config。**返回形状（G118）**：`{apiKey, baseUrl, model, projectDir, models}`——**camelCase 对齐 G115**（main 从 TOML 读后转驼峰，renderer 不感知 snake_case）；`models` = `[[llm.models]]` 的 name 列表（给 G111 的 model 下拉用，**复用 init 的 list_models 结果亦可**，但 getSettings 返回保证对话框独立可用）。**api_key 返回明文（G118）**：沙箱 renderer 可读（主进程信任 renderer，G102 纵深防御在写入侧），对话框 type=password 掩码显示（G51）；「已配置但为占位符 sk-...」时返回空串（G117 排除占位符）——用户看到空 key 输入框即知需填 |
| `emrg:cancel` | renderer→main | — | 发 cancel 到 daemon。**无参数（G24）**——daemon 端 cancel 不读 session_id，直接取消当前任务（daemon.py:283） |
| `emrg:event` | main→renderer | {type, data} | 推流式事件（delta/tool/done） |
| `emrg:pickProjectDir` | renderer→main | — | 首启选项目目录（**G70**）——main 用 `dialog.showOpenDialog({properties: ['openDirectory']})`（renderer 无 fs/dialog 权限），返回所选目录路径；确认后写入 config 的 `[gui] project_dir`（G6） |

**main 进程日志（G84）**：GUI main 进程日志写 `~/.emrg/emrg-gui.log`——对照 TUI 的 `emrg-client.log` 与 daemon 的 `emrgd.log`（RotatingFileHandler：10MB × 3，`emrg/server/__main__.py` 同款）。**打包后无终端，console.log 输出全丢**——main 内所有调试用 `console.log` 统一走日志封装（appendFile 或 electron-log 库），至少记录：连接事件（connect/auth_ok/close/error）、daemon spawn/退出、IPC 异常、未捕获异常（`process.on('uncaughtException')`）。renderer 的 console 经 `webContents.on('console-message')` 转发 main 日志（v1 可省略，先记 main 侧）。

### 4.3 preload API 形状（G67）

renderer 经 `window.emrg` 调用的 API（contextBridge 暴露），全部 promise 化（invoke/handle 模式）：

```javascript
// preload.js — contextBridge.exposeInMainWorld('emrg', ...)
window.emrg = {
  init: () => ipcRenderer.invoke('emrg:init'),              // → 初始状态
  sendMessage: (sessionId, text) => ipcRenderer.invoke('emrg:sendMessage', { sessionId, text }),
  listSessions: () => ipcRenderer.invoke('emrg:listSessions'),
  switchSession: (sessionId) => ipcRenderer.invoke('emrg:switchSession', { sessionId }),
  newSession: () => ipcRenderer.invoke('emrg:newSession'),
  deleteSession: (sessionId) => ipcRenderer.invoke('emrg:deleteSession', { sessionId }),
  setModel: (model) => ipcRenderer.invoke('emrg:setModel', { model }),
  listModels: () => ipcRenderer.invoke('emrg:listModels'),
  saveSettings: (config) => ipcRenderer.invoke('emrg:saveSettings', { config }),
  getSettings: () => ipcRenderer.invoke('emrg:getSettings'),
  cancel: () => ipcRenderer.invoke('emrg:cancel'),
  pickProjectDir: () => ipcRenderer.invoke('emrg:pickProjectDir'),   // G70
  onEvent: (cb) => {                                       // 事件订阅（ipcRenderer.on 包装）
    ipcRenderer.on('emrg:event', (_e, payload) => cb(payload));
  },
};
```

**注意（G67）**：renderer 侧 `onEvent` 收到的是 main 已分类的事件（`{type: 'message_delta'|'tool_started'|..., data}`）——**raw 帧分类只在 main 的 classify() 做一次**，renderer 不重复解析协议（架构原则 2：renderer 只渲染）。

### 4.4 命令-响应配对（G93，关键实现机制）

**问题**：daemon 协议是**异步响应**——`sendCommand("list_sessions")` 只发不读（对照 daemon_manager.py:194-200），响应 `sessions_list` 帧**不带 request_id**（daemon.py:713 `{"type": "sessions_list", "sessions": ...}`）。§4.2 各 IPC 通道的"返回"（listSessions 返回会话列表、init 返回初始状态…）**如何把 invoke 的 promise 和异步帧配对**？文档此前未定义——实施者会卡在这。

**方案：pending 命令队列（type 配对 + FIFO）**：

```javascript
// main 进程
const pending = new Map();  // frameType → { resolve, reject, timer }

function sendCommandAndWait(frameType, payload, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    pending.set(frameType, { resolve, reject,
      timer: setTimeout(() => { pending.delete(frameType); reject(new Error(`timeout waiting ${frameType}`)); }, timeoutMs) });
    ws.send(JSON.stringify({ type: payload.type, ...payload.params }));
  });
}
// classify() 里 list_result/command_result/pong 分支：查 pending 表 →
//   有匹配 → resolve + 清 timer；无匹配 → 广播给 renderer（如 model_set 广播帧）
```

**配对规则（G93+G103）**：
1. **按帧 type 配对 + FIFO**：同连接消息顺序保证（daemon 顺序处理）——发 list_sessions 后收到的第一个 `sessions_list` 帧即响应。**不允许多个同 type 命令并发未决**（GUI 逻辑保证：init 序列串行、listSessions 由 UI 事件串行触发）
2. **未决命令超时 5s** → reject → renderer 提示"操作超时"（daemon 可能忙/异常）
3. **广播帧不占 pending**：`model_set`/delta/done/tool_* 等广播帧**不注册 pending**（classify 直接推 renderer）；只有**显式等响应的命令**（ping/list_*/resume/delete/…）走 sendCommandAndWait。`resume_session` 的响应 `resume_result` 由 switchSession 通道 consume（promise resolve），**不同时推 renderer**（G13 meta 用途）
4. **断连时 pending 全部 reject**（重连循环会重新 list_sessions/resume，G41）
5. **error 帧配对（G103）**：daemon 的错误响应**无 type 字段**（`{"error": "session busy"}`，daemon.py:306/314/976）——**pending 表按 type 查不到**。规则：收到 error 帧时若 pending 表**有未决命令** → **FIFO reject 最早未决的那个**（daemon 顺序处理保证 error 是对最近命令的响应）→ renderer 拿到真实错误信息（如"会话不存在"而非"操作超时"）；pending 表为空 → error 是流式/广播上下文（如 session busy 是 task 拒绝）→ 推 renderer 按 G42 处理。**注意**：task 的 `session busy` 拒绝**不是命令响应**（sendTask 不注册 pending）→ 走广播分支（G42 ①）。

---

## 5. UI 设计（renderer）

### 5.1 布局（对齐 packaged-installer §7.1）

```
┌────────┬───────────────────────────────────────────────┐
│ 会话   │   [EMRG — 你好，我是你的 AI 编程助手]           │
│        │   （流式文本逐字出现…）                        │
│ ▼ 会话1 │                                               │
│  会话2  │   🔧 bash ls -la — 完成 0.4s                 │
│  会话3  │                                               │
│        ├───────────────────────────────────────────────┤
│  + 新建 │   [输入消息…                        [发送] ⏹] │
├────────┴───────────────────────────────────────────────┤
│  ● daemon 运行中 · deepseek-chat · 演化 12 次   [设置]  │
└────────────────────────────────────────────────────────┘
```

### 5.2 组件

| 组件 | 实现 | 说明 |
|------|------|------|
| ChatView | `<div>` + markdown.js | 流式增量：delta 追加到当前消息节点；工具调用折叠卡片 |
| SessionPanel | `<ul>` | list_sessions 填充；点击切换；右键删除。**显示名（G27）**：`title` 优先、`session_id` 兜底（对照 `session.py` `Session.title` = `meta.title or session_id`）；显示 message_count 副标签。**v1 无重命名（G35）**——title 由 TUI `/rename` 或 daemon 自动生成（rename_session 空 title 时 LLM 生成，daemon.py:741）填充；重命名入口留 v2。**⚠️ title 不会自动出现（G130）**：`_generate_session_title` **只在 `rename_session` 带空 title 时被调**（daemon.py:741）——而 TUI 只在 `/rename` 命令时发 rename_session（app.py:1335-1339）、**无人自动触发**。因此**新会话的 title 恒为 session_id 兜底显示**（G27），除非用户在 TUI 手动 `/rename`。GUI v1 不自动生成（对齐 TUI 现状，避免实施者误以为"daemon 会自动命名"）。**sessions_list 元素字段（G72）**：`session_id/created_at/updated_at/cwd/message_count/compact_count/last_compact_at/title`（title 可能缺失——G27 兜底；list_sessions 直接返回 meta.json，session.py:530-555） |
| ToolStatus | 工具消息卡片 | tool_start 显示运行中 → tool_end 更新结果 |
| SettingsDialog | `<dialog>` | api_key/base_url/model + **project_dir（G112）** 读写 config.toml（**经 main 进程用 smol-toml 解析/序列化**，G9——Node 无内置 TOML）。**api_key 输入 type=password（G51）**；config.toml 不存在时 getSettings 返回默认值（空 key + 默认 base_url，G52）。**project_dir 字段**：显示当前目录路径 + 「选择…」按钮（复用 `emrg:pickProjectDir`，G70）——G82"用户可在设置对话框改 project_dir"此前缺 UI 入口；G6-B 默认 `~/.emrg/evolution` 时也在此改。保存走 G60 全量读改写（含 `[gui] project_dir`） |

**model 切换双通道（G50+G111）**：设置对话框保存 model = 写 config.toml → daemon mtime 重启生效（G11）；`emrg:setModel` 是**运行时切换**（不写盘、不重启，对应 TUI `/model`）——v1 可不在 UI 暴露（保留通道与单测），避免"保存了 model 但走了 setModel 不生效"的实现歧义。**model 名校验（G111）**：daemon 的 set_model 只在 model ∈ {默认 model} ∪ `[[llm.models]]` 时解析 context_window/vision（daemon.py:1905-1915，任意名会保留旧 context_window）——**设置对话框的 model 下拉只列 `list_models` 返回的合法模型**（默认 + `[[llm.models]]`，daemon.py:1870-1887）；**不提供自由文本输入**（用户要加新模型 → 手动编辑 config.toml 或 v2 加"模型管理"）。api_key/base_url 自由文本；model 下拉 + 默认值选中。

**首启引导（G36+G71+G82，重设计）**：

> **⚠️ 前提事实（G71）**：`emrg/server/__main__.py:29` 在模块顶层直接 `load_config()`——`~/.emrg/config.toml` **不存在时 daemon 进程直接崩溃**（FileNotFoundError，非优雅退出）。因此首启引导**不能在 config 缺失时先拉起 daemon**。

```
main 启动 → 检查 ~/.emrg/config.toml 是否存在
├─ 不存在（全新安装）→ init 返回 {config_exists: false, api_key_configured: false}
│    → renderer 弹首启引导对话框（⚠️ 不调用 ensureConnected——避免拉起即崩的 daemon）
│      Step 1: 选项目目录（emrg:pickProjectDir，G70）→ 存 [gui] project_dir
│      Step 2: 填 API Key / base_url / model
│    → 保存 → main 用 smol-toml 写最小 config.toml（[llm] 段 + [gui] 段）
│      ⚠️ 优先方案（G116）：先 spawn python 一次性跑 `ensure_config()`（python -c "from emrg.config import ensure_config; ensure_config()"）
│      → 生成官方默认模板（含 [[llm.models]] 预置模型，config.py:99-130）→ smol-toml 改 api_key 等字段
│      → 自研写最小文件会丢失模板里的 [[llm.models]]（deepseek-v3/r1 预置，G111 的 model 下拉依赖它）
│    → 再调一次 init → config 存在 → 正常序列
└─ 存在 → ensureConnected → ping → list_sessions → 完整初始状态（api_key 为空则弹设置，G36）
```

> **G116（首启模板复用 ensure_config）**：TUI 客户端启动时调 `ensure_config()` 自动写默认 config.toml（client/__main__.py:12）——**GUI 首启保存前先复用该函数**（spawn python 一次性执行 `python -c "from emrg.config import ensure_config; ensure_config()"`，走 G59 的 .venv 定位）→ 生成官方模板（含 `[[llm.models]]` deepseek-v3/r1 预置，config.py:99-130）→ 再 smol-toml 改 api_key/base_url/model/[gui] project_dir。**自研写最小文件会丢 [[llm.models]] → G111 的 model 下拉只有默认模型一项**。**占位符坑（G117）**：模板 api_key 是 `"sk-..."` 占位符（config.py:108）——GUI 判断"是否已配 key"必须**排除占位符**（`api_key 为空 或 == "sk-..."` → 视为未配置）；否则用户从不改 key 也会被当已配置，首启引导跳过、实际 key 无效。

> **G82（首启含目录选择）**：G6 方案 A 的"首启选项目目录"必须落在首启引导里——全新安装时 config 缺失（G71 分支）正好是唯一一次必然走首启的机会。两步合一对话框（目录 + API 设置），用户可跳过目录（**跳过 → 默认 `~/.emrg/evolution` 兜底，G6-B**——不写 [gui] 段）。**`[gui] project_dir` 写入**：config.py 不解析 `[gui]` 段（tomllib 只取 `data.get("llm")`，Python 侧安全忽略），GUI 写入/更新它复用 G60 全量读-改-写（smol-toml 解析 → 设 `gui.project_dir` → 全量序列化写回，保留 `[llm]` 全部键与其它段）。main 每次读 config 取 `project_dir` 作为默认 cwd；**config 已存在但无 `[gui]` 段** → 用默认 `~/.emrg/evolution` 兜底 + 状态栏显示「默认目录」，用户可在设置对话框改。**字段名映射（G115）**：renderer 的 `projectDir`（camelCase，G114 白名单）→ main 落盘 `[gui] project_dir`（snake_case，TOML 惯例）——映射只在 main 的 saveSettings handler 做一次，renderer 不感知 TOML 键名。`pickProjectDir` 只返回用户所选路径（**不写盘**）——由用户点「保存」才经 saveSettings 落盘（对话框「取消」不丢旧值）。

**init 必须先行判断 config 存在性（G71）**——`emrg:init` 内部序列（G34）前置一步：config.toml 不存在时**跳过 ensureConnected/startDaemon**（拉起即崩），直接返回缺配置状态；renderer 据此弹首启设置。保存成功后 renderer 重新 `init()`。

**首启保存 vs 常规保存的重连差异（G123）**：G119"保存后不主动重连，等 ws close"**仅适用于常规保存（daemon 已在运行）**——首启保存（config 从无到有）时 **daemon 尚未运行、没有 ws 可 close**，必须**主动**走 ensureConnected（拉起 daemon）。区分：saveSettings 返回 `{daemonWasRunning: bool}`（main 在保存前用 isRunning TCP 探测）→ renderer 据此：wasRunning=true → 等重连事件；wasRunning=false → 主动 `emrg:init`（触发拉起）。**G71 流程图的"再调一次 init"即此主动路径**——此前 G119 的"不主动重连"会与首启衔接冲突，特此澄清。

**project_dir 失效处理（G121）**：daemon 的 `_get_or_create_session` 对**任意 cwd 路径**都执行 `mkdir(parents=True, exist_ok=True)`（session.py:55，Session 构造）——**GUI 传一个已被删除/移动的 project_dir，daemon 会在该路径自动重建目录并创建会话**（静默数据错位：会话文件出现在用户已不用的路径）。**main 侧防护（G121）**：`emrg:init` 时校验 `project_dir` 存在且可写（`fs.existsSync` + `fs.accessSync(W_OK)`）——不存在则：① 状态栏显示「⚠️ 项目目录不存在」；② **禁用发送**（输入条置灰）；③ 弹设置对话框引导改目录（G112 的 project_dir 字段）。**不自动 fallback 到 ~/.emrg/evolution**（用户可能误操作——显式提示优于静默换目录）。v1 不处理"中途目录被删"（发送时才校验，失败即提示）。

**「+ 新建」行为（G37）**：点击 → `emrg:newSession`（cwd 由 main 注入 project_dir）→ main 本地生成 `s_YYMMDD_HHMM_xxxx`（查重）→ 返回新 session_id → renderer 切换为新会话（聊天区清空 + 无占位）+ 刷新 SessionPanel。新建后首条消息即惰性创建 daemon 侧会话（`_get_or_create_session`）。

**空态与确认（G76）**：
- **会话列表空**（project_dir 无 .emrg/sessions 或空）：SessionPanel 显示「暂无会话」占位 + 引导「+ 新建」——不显示空列表（不可点击区域）
- **删除会话确认**：右键删除 → `confirm()` 对话框（「删除后不可恢复」）→ 确认后才发 `emrg:deleteSession`；删除当前会话后自动切换到剩余最近会话（无剩余则显示空态 + 聊天区欢迎语）
- **被动删除恢复（G106）**：`delete_session` 用 `self._send` 非 `_broadcast`（daemon.py:866-897）——**其他客户端（TUI `/delete`）删除会话时 GUI 收不到通知**，SessionPanel 残留已删条目。处理：switchSession 收到 `resume_result` 带 `error`（"Session not found"）→ **自动 list_sessions 刷新列表** + 提示"该会话已被删除，已切换到最近会话"（若有剩余）或空态。v1 不轮询列表（零成本可接受残留，点击即自愈）。
- **首启欢迎语**：聊天区在无会话时显示欢迎块（「你好，我是 EMRG——选择会话或新建开始」）——对齐 TUI welcome message
- **设置对话框**：无 api_key 时输入框聚焦 + 红色提示「API Key 必填」；保存时校验非空
| StatusBar | `<footer>` | daemon 状态点（绿/红）+ model + 演化计数 |

### 5.3 流式渲染（关键性能点）

**v1 策略（G8）——流式纯文本 + done 后整体 Markdown 渲染**：
```
sendMessage → UI 立即上屏用户消息（乐观渲染，G49）+ 清空输入条
message_delta → 追加到对应消息节点的 textContent（纯文本，不重建 DOM）
   - 自有流（request_id 匹配当前发送，G7）：当前 assistant 消息节点
   - 广播流（他人发起）：按 request_id 分组——新 request_id 建新节点并标注「来自其他客户端」（G3）
→ done 帧到达 → 对整个消息内容做一次 marked 渲染（替换纯文本节点）
→ 发送失败（session busy/断连）→ 消息下方显示错误行，不丢用户输入
```

> **G48（渲染节流）**：高频 delta 用 `requestAnimationFrame` 批量合并（一帧内多次 delta 只写一次 DOM）——对齐 TUI 60fps 渲染节流；纯文本 textContent 追加 + rAF 合并后，长回复（数万 token）不卡 UI。
> **G127（done 后渲染不阻塞）**：G8 的"done 后整体 marked 渲染"对**超长回复**（几十 KB markdown + 多个代码块）是同步主线程操作，一次可能阻塞 100-500ms（用户感知卡顿）。处理：`render()` 用 **`requestIdleCallback` 调度**（空闲时渲染，期间用户可滚动/输入）；渲染完成后节点替换。若 500ms 内无空闲（持续交互）→ 降级为**分片渲染**（先渲染前 8KB 文本节点，剩余 done 后空闲续渲染——但会破坏"一次性整体渲染"的原子性，v1 接受：长消息的代码块高亮可稍后补）。**简单 v1 方案（推荐）**：done 后 50ms 内若 rIC 不可用（老 Chromium）→ 直接同步渲染（接受一次性卡顿，消息长度上限 200KB 兜底，G91 保证不超）。
> **G122（IPC 批量推送）**：main→renderer 的 `emrg:event` **每条 delta 一次 send** 在高速流下是瓶颈（IPC 消息开销 ~μs 级但高频累积；renderer 侧 rAF 合并的前提是消息已到）——main 侧对 `message_delta` 做 **16ms 批量**（合并一窗口内多条 delta 为一条 event `{type:'message_delta_batch', data:{requestId, chunks:[...]}}`，或简单点：main 累积 16ms 后一条 event 带 chunks 数组）。tool_*/done/error 等低频事件**不批量**（即时推）。renderer 的 rAF（G48）在批量之上再合并 DOM 写。两层合并后长回复（数万 delta）IPC 消息量降 ~60 倍。

**为什么不做流式中实时 Markdown**：
1. delta 是纯文本，实时 marked 解析需**重建 DOM**——光标/滚动位置丢失，高频 delta 卡顿
2. **代码块未闭合时 marked 输出不可用**（``` 没结束 → 整段被当代码块）
3. 纯文本追加 O(1)，marked 渲染只在 done 时一次

工具调用（tool_start/tool_end）是独立节点，不参与流式文本——tool_start 建卡片，tool_end 更新状态，不受 Markdown 策略影响。

**大内容保护（G91+G131）**：
- **tool_end content 截断显示**：工具输出实际有上限（bash 200KB/bash_tool.py:16、read 256KB/read_tool.py:17-18、grep 200 条/grep_tool.py:18）——但 200KB 渲染仍卡，卡片默认只显示**前 2000 字符** + 「…（共 N 字符）」+ 点击展开/折叠；error 内容同样截断
- **IPC 传输全量 + DOM 只放截断（G131）**：`emrg:event` 推 tool_end 时**传输全量 content**（上限 200KB，Electron 结构化克隆 ~1ms 内，可接受）；renderer **存储全量**（JS 变量/隐藏字段）但 **DOM 只插入前 2000 字符**；点击展开时再插入全量——**用 textContent 纯文本**（工具输出不做 marked 渲染，防 XSS 面扩大 + 性能）。**main 侧不截断**（截断是显示层决策，main 保持数据完整）
- **assistant 消息 DOM 上限**：v1 无历史加载（G12），但**长会话**（数百条消息）DOM 节点膨胀——简单策略：聊天区保留**最近 200 条消息节点**，超出丢弃最老的（滚动回看 v1 不做，对齐"GUI 极简"）
- **后台窗口流式（G92）**：webPreferences 设 `backgroundThrottling: false`——Chromium 对后台窗口默认节流 rAF（每秒 1 帧），流式渲染会明显变慢；关掉后后台窗口流式不延迟（代价：后台 CPU 略增，可接受——流式本就有节流 G48）

**自动滚动**：流式中滚到底部（用户上翻时暂停）。

**markdown.js 职责（G74+G132）**：`render(content) → HTMLElement`——marked（gfm: true, breaks: false）+ DOMPurify 消毒 + highlight.js 高亮。**done 后调用一次**，替换流式纯文本节点。**highlight.js 主题 CSS 需本地打包**（`styles.css` 引入 `node_modules/highlight.js/styles/github-dark.css` 或复制进 `renderer/`——CSP `style-src 'unsafe-inline'` 允许内联但外部 css 必须本地文件，禁 CDN，G26）。代码块语言用 `highlight.js` 自动检测或 marked 的 langPrefix 映射。**语言子集（G132）**：highlight.js 全量注册 190+ 语言（打包体积 ~1MB+，Electron 体积敏感）——**只注册常用子集**（`javascript/typescript/python/bash/json/yaml/markdown/html/css/go/rust/java/c/cpp/sql` 等 ~20 种，`highlight.js/lib/languages/*` 按需 import）；未注册语言的代码块**降级为纯文本**（无高亮，不报错）。**`hljs.highlightAuto` 慎用**（自动检测对短代码块误判率高）——优先按 marked 的 lang 属性精确高亮，无 lang 才用 `highlightAuto` 兜底。

**输入条行为（G75）**：
- **Enter = 发送，Shift+Enter = 换行**（非开发者直觉，避免误发多行）
- 流式运行中禁用发送（输入条置灰 + placeholder「回复生成中…」）——对齐 G65 会话锁
- 空文本/纯空白 → 忽略（不发空 task）
- 发送后清空输入条（G49）；**发送失败恢复输入框内容**（session busy/断连时用户文本不丢，仅乐观节点加错误行）

### 5.4 历史加载（G12，v1 裁剪）

**实测**：`resume_session` **不返回历史**（daemon.py:1952 注释 "The client reads history.jsonl directly from disk for display"）；TUI 切换会话时直接读磁盘 `cwd/.emrg/sessions/<sid>/history.jsonl`（app.py:716）。`list_history` 消息只返回 **user 消息预览**（截断 80 字），不含 assistant 回复/工具调用——不足以重建聊天视图。

**v1 决策：不加载历史（G12-C）**：

```
切换会话 → resume_session → 拿到 meta（message_count）
→ 聊天区显示占位：「会话已加载（N 条消息）——历史回放 v2 支持」
→ 新消息正常流式显示（广播模型照常）
```

**v1 不调用 `list_history`（G15）**——该消息只返回 user 消息预览（截断 80 字），不足以重建聊天视图；历史回放 v2 才需要（届时 daemon 扩展返回完整记录）。

**流式进行中切换会话（G65）**：
- **v1 决策：流式运行中禁用会话切换**（SessionPanel 点击无效 + 提示"任务运行中"）。原因：① 切换须发 resume_session → daemon 立即把订阅移到新会话 → 旧流的 delta/done 帧**不再到达本连接**，切回时 v1 不加载历史（G12）→ 结果永久看不到；② cancel 是连接级（G33），切走后再按 ⏹ 会取消**仍在后台跑的任务**（历史也不写全）。
- 允许切回原会话等流结束后操作；或先 ⏹ 结束当前流再切换。
- **例外**：广播流（他人发起，非自有 request_id）不锁定 UI——可正常切换（任务不依赖本连接）。
- **⚠️ 切换时清理分组（G110）**：广播分组缓存（G83/G104）**按 session 隔离**——切换会话（switchSession）时**清空旧 session 的分组**（含超时 timer）。理由：① done 帧带 `session_id`（G63），新 session 的帧可能与旧分组 request_id 无关；② G83 的 10 分钟超时兜底会让**已切走的会话**残留分组占用内存。切换后旧分组节点在 DOM 里保留（已完成消息不删），只清缓存（G83 语义：分组缓存删、消息节点留）。若切回原会话，旧流已死（订阅移走过，G66）——不补建分组。

**为什么**：
1. **renderer 无 fs 权限**（架构原则 2）——不能直接读磁盘
2. 让 main 读磁盘解析 history.jsonl = **跨语言重写 TUI 的磁盘解析逻辑**（重复劳动）
3. 改 daemon 返回完整历史 = **动 server**（违反"零改动"原则）
4. 对齐"GUI 极简 v1"定位——历史回放是增强，非核心

**v2 候选**：daemon 扩展 `list_history` 返回完整记录（assistant + tool），GUI 经 IPC 获取；或 main 读磁盘（复用协议契约的 history.jsonl 格式）。不在本文。

---

## 6. 测试策略

### 6.1 单测（daemon_client.js，Node 原生可测）

**`emrg/gui/test/daemon_client.test.js`**（**node:test，定案（G99）**——零依赖、Electron 内置 Node ≥18 原生支持；不引 vitest 减少 devDeps/构建面）：
- ensureConnected：port 文件读取 + auth 首帧 + auth_ok 处理
- 坏 JSON 帧 → 忽略不崩
- ws close → 触发重连回调
- sendTask：验证 payload（type=task + session_id + prompt + images + **id + stream:true**）
- sendCommand：验证 payload（type + params）
- **帧分类（G21+G58）**：tool_start/tool_end/delta/done/cancelled/error/pong/list_result/command_result 各帧正确分类（classify() 单测）——含 resume_result/model_set/session_deleted 落 command_result 而非 unknown
- sendCommand("cancel")：payload 无参数（G24）——单测断言 `{"type": "cancel"}` 无多余字段
- session busy 错误 → 事件分发正确
- **generateSessionId（G28+G81）**：格式 `s_YYMMDD_HHMM_xxxx` + 查重（mock fs.existsSync）+ **100 次碰撞返回 8-hex 兜底**（不返回 undefined）
- **startDaemon（G28）**：spawn 参数正确（源码 .venv/bin/python -m emrg.server / 打包 resourcesPath/emrgd；Windows 路径分支 G61；spawn 选项 detached/stdio G68）
- **isRunning（G28+G90）**：**TCP 探测**（mock net.connect 成功/拒绝 → True/False）——⚠️ 不是 port 文件存在性（G43 已改探测语义，此单测描述随 G28 过时，现修正）
- **auth 失败（G90）**：connect 成功但 auth_ok 前 ws close → 判定认证失败 → **停止自动重试**（G88）
- **断连 pending 请求（G90）**：流式中 ws close → 消息标记「连接中断」+ 输入条恢复（G89）
- **命令-响应配对（G93+G103）**：list_sessions 后收 sessions_list → promise resolve；无响应 5s → reject；**error 帧（无 type）→ FIFO reject 最早未决命令**；无未决命令的 error → 广播事件
- **分组生命周期（G83+G104）**：新 request_id 的 tool_start/delta → 建分组；done → 清理；10 分钟无帧 → 超时清理；>20 活跃 → 丢最老；**tool_start 建分组后 tool_end 更新卡片**

### 6.2 集成测试（真实 daemon，隔离环境）

**`emrg/gui/test/integration.test.js`**（⚠️ 范围：**协议往返**，sendTask 流式留 E2E——Node 无法注入 Python mock LLM，G5）：

> **⚠️ 隔离方案（G73，重写）**：daemon 的 `config_dir()` 硬编码 `Path.home()/'.emrg'`（config.py:45-47），**没有 config 路径环境变量**；Python 侧测试隔离靠**进程内 monkeypatch**（test_ws_e2e.py:63-67 `daemon_mod.config_dir = lambda: tmp`），**Node 子进程无法 monkeypatch**。因此：
>
> ```javascript
> // 集成测试 spawn 时注入 HOME（POSIX）/ USERPROFILE（Windows）指向临时目录
> const tmp = fs.mkdtempSync(os.tmpdir() + '/emrg-gui-test-');
> fs.mkdirSync(path.join(tmp, '.emrg'), { recursive: true });
> // 必须预写 config.toml（假 key + 假 base_url）——否则 daemon 启动即崩（G71）
> fs.writeFileSync(path.join(tmp, '.emrg', 'config.toml'),
>   `[llm]\nbase_url = "http://localhost:9999/v1"\napi_key = "test-key"\nmodel = "test-model"\n`);
> const child = spawn(pythonPath, ['-m', 'emrg.server'], {
>   env: { ...process.env, HOME: tmp, USERPROFILE: tmp },  // Path.home() 尊重 $HOME
>   stdio: 'ignore', detached: true,
> });
> // → daemon 的 config_dir() = tmp/.emrg（config/port/logs/projects.yml 全隔离）
> ```
>
> `Path.home()` 在 POSIX 读 `$HOME`、Windows 读 `$USERPROFILE`——注入后 daemon 完全落在临时目录，**零污染真实 ~/.emrg、不碰真实 api_key**。测试结束 kill daemon（测试自己起的，可安全杀）+ 清理 tmp。
>
> **pythonPath 来源（G126）**：复用 G59/G61 同款定位逻辑——项目根 `.venv/bin/python`（POSIX）/ `.venv\Scripts\python.exe`（Windows）；`.venv` 不存在时 PATH 兜底 `python3`/`python`。**集成测试不测 daemon 拉起的完整路径**（ensureConnected 的 startDaemon 已由单测 G28 覆盖 spawn 参数）——集成测试**直接 spawn**（已知 pythonPath）更稳。**skills 目录缺失容忍（G126）**：daemon 启动 `load_skills()` 对缺失的 `~/.emrg/skills` 和 `project_dir/.emrg/skills` 容忍（loader.py:112 `if user_dir.is_dir()`）——HOME 注入后 skills 目录不存在**不会崩**，返回空列表，集成测试无需预建。
>
> **范围（全部只读或隔离内操作）**：
- daemon_client.ensureConnected → sendCommand("ping") → 收到 pong（ServerPong 结构：identity/uptime_seconds/evolution_count）
- sendCommand("list_sessions") → sessions_list 响应（空列表）
- sendCommand("list_models") → models_list 响应
- sendCommand("list_projects") → projects_list 响应（空）
- session 操作：newSession（本地生成 sid）→ sendTask 不跑（G5），改 resume_session（不存在 → resume_result + error 路径）/ delete_session（不存在 → session_deleted + error 路径）——**验证错误帧结构，无破坏性**
- daemon 被杀（kill 测试 spawn 的子进程）→ ensureConnected 重连成功（隔离环境内验证 G43 stale port 流程）

> **sendTask 流式不在此层**（需真实 LLM 或 Python 侧 mock 注入，Node 测试进程无法注入）。方案：
> - E2E（Playwright for Electron）用真实配置手动验证流式；或
> - 后续 daemon 加测试模式环境变量（如 `EMRG_MOCK_LLM=1`）时补此层

### 6.3 E2E（Electron）

**冒烟**（手动或 Playwright for Electron）：
- 启动 → 首启引导（无 api_key 弹设置）
- 填 key → 聊天 → 流式显示 + 工具状态
- 会话切换/新建/删除
- daemon 被杀 → 状态栏红 → 自动重连
- TUI + GUI 同开同 session → 广播一致
- **设置保存 → daemon 重启 → 状态栏 model 刷新（G113）**：改 model → 保存 → 状态栏显示"配置已保存，daemon 重启中…" → 重连后 model 名更新（G98 验证点）
- **广播流 + 会话切换（G113）**：GUI 会话 A 被动接收 TUI 流式 → 中途切到会话 B → 分组缓存清理（G110）→ 切回 A 无"幽灵"状态
- **大工具输出（G113）**：`bash "seq 1 50000"`（或 cat 大文件）→ 工具卡片截断显示（G91，2000 字符 + 展开）→ 流式不卡、无断连（G105 16MB 验证）

### 6.4 全量回归

- 现有 pytest（464）不受影响（GUI 是纯增量，不动 Python 核心）
- Node 测试纳入 CI（`npm test`）——**G55+G100**：`.github/workflows/test.yml` 需补 `actions/setup-node` + `npm ci` + `npm test`，**工作目录 `emrg/gui/`**（`working-directory: emrg/gui`——G99 测试已放该目录，node:test 默认发现 `test/*.test.js`）；`npm ci` 前需 `cd emrg/gui` 有 package-lock.json（G79）
- **集成测试在 CI 的条件（G100）**：`integration.test.js` 要 spawn Python daemon（G73 注入 HOME）→ 依赖 .venv/python + emrg 包——CI 中 `uv sync` 已建 .venv（test.yml 现有步骤），Node job 复用（`../.venv/bin/python` 相对 `emrg/gui/` 上溯两级 = 项目根 .venv，G59 同款定位）。**或 CI 拆分**：单测必跑，集成测试标记 skip（`EMRG_SKIP_INTEGRATION=1`）留本地——v1 建议单测必跑 + 集成测试本地跑，CI 先跑单测（Electron 打包流水线另有冒烟，packaged-installer §）
- Playwright E2E 先手动跑，CI 加装 Chromium 的成本 v1 可不引入（G55）

---

## 7. 验收标准

- [ ] `emrg/gui/` 目录结构落地（package.json/main.js/preload.js/daemon_client.js/renderer/）
- [ ] `npm start` 启动 GUI → 自动拉起 emrgd → 连接成功 → 状态栏绿色
- [ ] 首启引导（G71 流程）：**config.toml 缺失** → 弹设置（不拉起 daemon）→ 填 key 保存 → 自动拉起 daemon → 可聊天；config 存在但 api_key 空 → 弹设置（G36）
- [ ] 聊天流式：发送 → delta 逐字显示 → 工具调用状态行 → done 结束
- [ ] 会话：列表显示 / 切换 / 新建 / 删除（与 TUI 操作同一 daemon，数据一致）；流式运行中禁止切换（G65）
- [ ] 断连重连：daemon 被杀 → 状态栏红 → 自动拉起（stale port 处理，G43）→ 重连 → 会话恢复（重新 resume，G41）
- [ ] TUI + GUI 同开同 session：A 发消息 B 实时收到（广播模型验证）
- [ ] daemon_client.js 单测覆盖（node:test）全绿；集成测试过隔离 daemon（G73——注入 HOME 临时目录，不碰真实 ~/.emrg）
- [ ] `emrg/gui/` 零改动 emrg/server、emrg/connect、emrg/client（纯增量）
- [ ] Python 核心 pytest 464 全绿（无回归）

---

## 8. 与 Phase 4（打包）的衔接

- **electron-builder** 打包 `emrg-gui`（.dmg/.exe/.AppImage），TUI/daemon 仍 PyInstaller（双轨）
- `main.js` 的 `sys.frozen` 分支：打包后 spawn 同目录 `emrgd` 二进制（非 `python -m emrg.server`）
- **emrgd 分发结构（G17+G38）**：PyInstaller 为 **onedir 模式**（packaged-installer §"onedir 模式（daemon 长期运行 + 更新替换需要）"）——`emrgd` 是**目录**（含二进制 + `_internal/`），非单文件。electron-builder `extraResources` **递归复制整个目录**进 `resources/emrgd/`，spawn 路径：
  - macOS/Linux：`path.join(process.resourcesPath, 'emrgd', 'emrgd')`
  - Windows：`path.join(process.resourcesPath, 'emrgd', 'emrgd.exe')`
  - 打包结构：`emrg-gui.app/Contents/Resources/emrgd/emrgd` + `.../emrgd/_internal/`（Windows 同理 `resources/emrgd/`）
- **electron-builder 配置（G30）**：`package.json` 的 `build` 段——`appId: com.emrg.gui`、`productName: EMRG`、`extraResources: [{from: '../dist/emrgd', to: 'emrgd'}]`（emrgd 来自 PyInstaller onedir 产物目录）、三平台 target（dmg/exe/AppImage）
- **图标（G39）**：electron-builder `icon` 指向 `../packaging/assets/`（icns/ico/png 多尺寸，与安装器共用，packaged-installer §7 已有）——`emrg/gui/` 下不再维护重复图标
- asar 打包 renderer；`daemon_client.js` 在 main（不进 asar 也可，随包）
- **asar 与 __dirname（G78）**：electron-builder 默认 `asar: true`——**打包后 `__dirname` 指向 `resources/app.asar/emrg/gui`，不是真实文件系统路径**。但 G59 的 `__dirname` 上溯定位 .venv 只在**开发模式**（app.isPackaged=false）使用；打包后走 `process.resourcesPath/emrgd` 分支（G17）——两条路径互斥，asar 无冲突。**注意**：`daemon_client.js`/`main.js` 若需读 renderer 外的资源（如配置模板），一律用 `process.resourcesPath`（真实路径），禁 `__dirname` 拼 app.asar 内路径。
- **依赖锁定（G79）**：提交 `package-lock.json`（CI 用 `npm ci` 而非 `npm install`，G55 一致）；`ws`/`smol-toml`/`marked`/`highlight.js`/`dompurify` 全为纯 JS，无原生编译——electron-builder 打包无需 rebuild（无 native module）。
- **签名（G80）**：v1 沿用 packaged-installer §"签名与公证"约定——macOS Developer ID + notarytool 公证（无证书时 CI 跳过，产物标注 unsigned）；Windows EV 证书可选；Linux 无签名。electron-builder 的 mac 签名需 `build.mac.identity` 配置，未配置时自动跳过。

---

## 9. 决策记录

1. **Electron 技术栈**（2026-08-03 用户决策）：main Node + renderer Chromium；取代 PySide6。
2. **main 进程是唯一 daemon 连接者**：renderer 零网络权限（contextBridge 安全沙箱）——安全 + 单一连接管理点。
3. **daemon_client.js 对照 daemon_manager.py 写**：复制协议语义（auth/坏 JSON/断连/session busy），不复制 Python 代码。
4. **UI 极简 v1**：聊天/会话/工具状态/设置；diff/图片/记忆浏览留 v2（对齐 MANIFESTO 第十四条）。
5. **流式渲染 v1 策略（G8）**：流式中纯文本追加（textContent，不重建 DOM），done 后整体 marked 渲染——避免 DOM 重建 + 未闭合代码块问题。
6. **纯增量**：零改动 emrg/server、emrg/connect、emrg/client（Python 核心）。
7. **Node 测试进 CI**：单测（mock ws）+ 集成（真实 daemon）+ E2E（Playwright for Electron）三层。

### 修订记录（review v1 G1-G5 全部采纳）

| # | 修订 |
|---|------|
| G1 | §3.3 补帧分类逻辑：TaskResponse 帧无 type 字段，靠 delta/done/request_id 判断（附 classify() 代码） |
| G2 | §3.3 补 done 三形态（正常/LLM 错误/取消），不得清空已显示内容 |
| G3 | §3.3 补广播场景：他人发起的帧按 request_id 分组显示 + 被动接收流 |
| G4 | §3.3 补 cancelled 帧处理 + list_result 事件归类 |
| G5 | §6.2 集成测试范围明确为协议往返（Node 无法注入 Python mock LLM），流式留 E2E 或 daemon 测试模式 |

### 修订记录（review v2 G6-G8 全部采纳）

| # | 修订 |
|---|------|
| G6 | §2.2 补 cwd 来源设计：首启选项目目录存 config（`[gui] project_dir`），默认 `~/.emrg/evolution` 兜底；会话级多目录留 v2 |
| G7 | §3.1 补 sendTask 内部生成 request_id（uuid）并缓存为"当前发起流"，done 时比对清除；非匹配帧 = 广播流（他人发起） |
| G8 | §5.3 流式渲染改 v1 策略：流式纯文本追加 + done 后整体 Markdown 渲染（避免 DOM 重建 + 未闭合代码块）；决策记录 5 同步 |

### 修订记录（review v3 G9-G11 全部采纳）

| # | 修订 |
|---|------|
| G9 | §2.2 package.json 补 `smol-toml`（Node 无内置 TOML）；§5.2 SettingsDialog 注明经 main 用 smol-toml 读写 |
| G10 | §3.2 补 `const WebSocket = require('ws')`（Electron 内置 Node 无原生 WebSocket，Node 21+ 才有） |
| G11 | §4.2 saveSettings 补 daemon 重启断连提示（mtime 检测 → §3.4 重连流程） |

### 修订记录（review v4 G12-G13 全部采纳）

| # | 修订 |
|---|------|
| G12 | 新增 §5.4 历史加载：resume_session 不返回历史（daemon 注释实证）；v1 决策不加载历史（切会话显示 meta 占位），完整回放留 v2（daemon 扩展或 main 读磁盘） |
| G13 | §4.2 switchSession 返回改"resume 结果 + meta（消息数）"，去掉"历史"（resume 不返回历史） |

### 修订记录（review v5 G14-G15 全部采纳）

| # | 修订 |
|---|------|
| G14 | §4.2 emrg:newSession 改为**本地生成 session_id**（daemon 无 new_session 消息，对照 session.py 格式 + 查重）；§3 补 generateSessionId() 代码 |
| G15 | §5.4 注明 v1 不调用 list_history（历史回放 v2 才需要） |

### 修订记录（review v6 G16-G18 全部采纳）

| # | 修订 |
|---|------|
| G16 | §3.2 补 startDaemon Python 路径：源码运行 .venv/bin/python（EMRG_ROOT 定位），打包后 process.resourcesPath/emrgd |
| G17 | §8 补 emrgd 分发结构：electron-builder extraResources → resources/，GUI 用 process.resourcesPath 定位 |
| G18 | §3.1 sendTask 注明 images 为 v2 预留（v1 恒 null，无图片 UI） |

### 修订记录（review v7 G19-G21 全部采纳）

| # | 修订 |
|---|------|
| G19 | §3.2 补 ping 时机：连接/重连成功后发一次（拿状态栏数据），不做轮询 |
| G20 | §3.3 补 tool_call_id 关联（tool_start 建卡片存 id，tool_end 匹配更新）+ tool_end error 红色显示 |
| G21 | §6.1 补帧分类单测（tool_start/tool_end/delta/done/cancelled/error/pong/list_result） |

### 修订记录（review v8 G22-G24 全部采纳）

| # | 修订 |
|---|------|
| G22 | §4.2 sendMessage 注明 cwd 由 main 从 config.project_dir 注入（renderer 不传） |
| G23 | §2.2 目录结构改 §2.3（修复与"cwd 来源"的编号重复） |
| G24 | §4.2 cancel 注明无参数（daemon 端不读 session_id，直接取消当前任务） |

### 修订记录（自动 review R1 G25 采纳）

| # | 修订 |
|---|------|
| G25 | §4.2 补 `emrg:listModels` 通道（设置对话框显示可用模型，daemon 返回 models_list） |

### 修订记录（自动 review R2 G26 采纳）

| # | 修订 |
|---|------|
| G26 | §4.1 补 CSP（Content-Security-Policy + Markdown 消毒防 XSS）；补 config 路径来源（os.homedir()/'.emrg'） |

### 修订记录（自动 review R3 G27 采纳）

| # | 修订 |
|---|------|
| G27 | §5.2 SessionPanel 补显示名策略：title 优先、session_id 兜底（对照 session.py:190-198）+ message_count 副标签 |

### 修订记录（自动 review R4 G28 采纳）

| # | 修订 |
|---|------|
| G28 | §6.1 单测补 generateSessionId（格式+查重）、startDaemon（spawn 参数）、isRunning（port 文件判定） |

### 修订记录（自动 review R5-R8 G29-G59 采纳）
| # | 修订 |
|---|------|
| G29 | 第 6 行补修订标注（v9 + 自动 review R1-R4） |
| G30 | §8 补 electron-builder build 配置段（appId/productName/extraResources 具体写法） |
| G31 | §3.2 ensureConnected 补 function 关键字（独立函数语法） |
| G32 | §3.1 sendTask 补 request_id 必须作为 task 帧 id 字段发出（daemon 只回显 req.id 不自生成，否则流匹配失效） |
| G33 | §3.3 补 cancel 每连接语义：只取消本连接发起任务；广播流 ⏹ 无效 → UI 只在自有流显示 ⏹；cancelled 帧不广播 |
| G34 | §4.2 emrg:init 补内部序列（ensureConnected → ping → list_sessions）；listSessions 补 cwd 由 main 注入 |
| G35 | §5.2 SessionPanel 明确 v1 无重命名（title 来源：TUI /rename 或 daemon 自动生成），入口留 v2 |
| G36 | §5.2 补首启引导流程（init 返回 api_key_configured → 无 key 弹设置禁输入 → 保存后 daemon 重启重连）——此前仅测试/验收有此项 |
| G37 | §5.2 补「+ 新建」行为（emrg:newSession → 本地生成 session_id → 切换+刷新列表） |
| G38 | §8 emrgd 分发改为 onedir 目录结构（含 _internal/，对照 packaged-installer "onedir 模式"）：extraResources 递归复制整个目录，spawn 路径 emrgd/emrgd（Win: emrgd.exe） |
| G39 | §8/§2.3 图标路径统一为 packaging/assets/（electron-builder icon 指向该目录），消除与 packaged-installer 的路径冲突 |
| G40 | §2.1 补窗口关闭语义：关窗只断 ws、daemon 继续运行；window-all-closed 按 Electron 惯例（macOS dock 保留） |
| G41 | §3.4 重连流程补"重新 resume 当前会话"——广播订阅是连接级（按 ws），重连即丢，不恢复则收不到该会话消息 |
| G42 | §3.3 error 帧补两类区分：session busy（无流）立即恢复输入条 vs 流中错误等 done |
| G43 | §3.2/§3.1 补 stale port 文件处理：ws 连接失败须删 port 文件 + startDaemon 拉起；isRunning 必须 TCP 探测，不可简化为 port 文件存在 |
| G44 | §4.1 Markdown 消毒改 DOMPurify——marked 的 sanitize 选项 v8 已移除，勿写 |
| G45 | §4.1 补 sandbox: true（preload 仅 ipcRenderer，无需 Node API） |
| G46 | §4.1 补外部导航拦截：setWindowOpenHandler deny + 链接经白名单协议 shell.openExternal |
| G47 | §5.3 流式节点归属补 request_id 分组交叉引用（自有流 → 当前节点；广播流 → 分组节点，G3） |
| G48 | §5.3 补 rAF 渲染节流（高频 delta 批量合并写 DOM，对齐 TUI 60fps） |
| G49 | §5.3 补发送乐观渲染：sendMessage 后立即上屏用户消息 + 清空输入条；失败在消息下显示错误行 |
| G50 | §5.2 补 model 切换双通道：设置对话框保存=写盘+重启生效；emrg:setModel=运行时切换（v1 可不暴露 UI），消除实现歧义 |
| G51 | §5.2 补 api_key 输入掩码（type=password） |
| G52 | §5.2 补 config.toml 不存在时 getSettings 返回默认值（空 key + 默认 base_url） |
| G53 | §2.3 package.json 依赖补全：dompurify（G44）+ devDeps（electron/electron-builder/测试框架）+ scripts 清单 |
| G54 | §2.3 补 npm scripts（start/test/test:integration/e2e/dist）；`npm start` 工作目录明确为 `emrg/gui/` |
| G55 | §6.4 补 CI 变更：test.yml 加 setup-node + npm ci + npm test；Playwright E2E 先手动 |
| G56 | 头部修订标注更新（R1-R17/G1-G59）+ §9 表标题（R5-R8） |
| G57 | §3.3 classify() 补 command_result：resume_result/model_set/session_deleted/clear_result 原落 unknown——switchSession 的 meta 拿不到；model_set 为广播帧（daemon.py:409）→ 状态栏 model 自动同步 |
| G58 | §6.1 单测补 command_result 分类断言 + cancel 无参数 payload 断言 |
| G59 | §3.2 删虚构的 EMRG_ROOT env，改 __dirname 上溯两级定位项目根（对照 Python 侧 __file__ 定位） |
| G67 | 新增 §4.3 preload API 形状：window.emrg 全量方法签名（invoke/handle promise 化）+ onEvent 订阅包装；明确 renderer 不重复做帧分类 |
| G70 | §4.2 补 emrg:pickProjectDir 通道（main 用 dialog.showOpenDialog 选目录，renderer 无 fs/dialog 权限） |
| G71 | **首启死锁修复**：daemon 在 config.toml 缺失时启动即崩（__main__.py:29 顶层 load_config）——init 必须先检查 config 存在性，缺失则不拉起 daemon、直接弹首启设置，保存后再 init |
| G65 | §5.4 补流式进行中切换会话策略：v1 禁用切换（订阅被 resume 移走后旧流 done 错过 + cancel 连接级会真取消后台任务）；广播流不锁定 |
| G66 | §4.2 switchSession 注明订阅切换副作用：必须发 resume_session 才能把连接订阅移到新会话（daemon.py:270-278 按消息 session_id 切订阅） |
| G61 | §3.2 startDaemon 补跨平台 python 路径（Windows .venv\Scripts\python.exe）+ .venv 缺失时 PATH 兜底（python3/python） |
| G62 | §3.2 补 ws maxPayload（Node 默认 100MB，Python 接收 16MB）——后经 G105 修正为 64MB（接收侧容错工具大输出） |
| G68 | §3.2 补 spawn 选项：stdio:'ignore' + detached:true + unref()（对照 DEVNULL + start_new_session=True，GUI 退出不带走 daemon） |
| G63 | §3.3 补 done 帧字段：正常 done 含 delta:false + session_id（daemon.py:1469-1474）；取消 done 无 delta 字段（daemon.py:1163-1168）；取消形态为 cancelled 帧 + done 帧两段 |
| G64 | §3.3 补 auth_ok 由 ensureConnected 消费不进事件流（对齐 connect.py:71-77），classify 不识别 auth_ok |
| G72 | §5.2 补 sessions_list 元素字段清单（session.py:530-555 直接返回 meta.json，title 可能缺失） |
| G60 | §4.2 saveSettings 补全量读-改-写（smol-toml 保留 [llm] 全部键 + 其它段，只改三字段）——只写三字段会丢配置 |
| G69 | §4.2 saveSettings 补 config.toml 写入 mode 0o600（含明文 api_key） |
| G73 | §6.2 集成测试隔离方案重写：daemon 无 config 路径环境变量、Node 子进程无法 monkeypatch——spawn 注入 HOME/USERPROFILE 指向临时目录 + 预写 config.toml（假 key）；范围限只读/隔离内操作；原"临时 config dir"方案不可行 |
| G74 | §5.3 补 markdown.js 职责（marked gfm + DOMPurify + highlight.js；主题 CSS 本地打包禁 CDN，CSP 约束） |
| G75 | §5.3 补输入条行为：Enter 发送 / Shift+Enter 换行；流式运行中禁用发送；空文本忽略；失败恢复输入框内容 |
| G76 | §5.2 补空态与确认：会话列表空占位、删除 confirm 确认、删除当前会话后自动切换、首启欢迎语、设置 api_key 必填校验 |
| G77 | §7 验收标准对齐 G71/G73：首启改 config 缺失流程；断连重连补 stale port + 重新 resume；集成测试改隔离 daemon |
| G78 | §8 补 asar 与 __dirname 说明：打包后 __dirname 指向 app.asar 内非真实路径——但 __dirname 定位 .venv 仅开发模式用，打包走 resourcesPath 分支，互斥无冲突；资源读取一律 process.resourcesPath |
| G79 | §8 补依赖锁定：package-lock.json 提交 + npm ci；全依赖纯 JS 无 native module 无需 rebuild |
| G80 | §8 补签名约定：macOS Developer ID + notarytool（无证书跳过标 unsigned）、Windows EV 可选、Linux 无；build.mac.identity 未配置自动跳过 |
| G81 | §3.2 generateSessionId 补 100 次碰撞后的 8-hex 兜底（对照 Python session.py:45-46）——原实现返回 undefined → daemon 报 task requires session_id |
| G82 | §5.2 首启引导补目录选择步骤（G6-A 落地）：config 缺失时两步合一（选目录 + API 设置），跳过则默认 ~/.emrg/evolution 不写 [gui]；[gui] 段 config.py 不解析（Python 安全忽略），写入复用 G60 全量读改写；config 无 [gui] 段 → 默认目录 + 状态栏提示 |
| G83 | §3.3 补广播分组生命周期：done/cancelled 即清理分组缓存；10 分钟超时兜底 + 20 活跃分组上限（防内存泄漏/广播风暴） |
| G84 | §4.2 补 main 进程日志：~/.emrg/emrg-gui.log（RotatingFileHandler 10MB×3，对照 emrgd.log）；打包后无终端 console 全丢；记录连接/spawn/IPC 异常/未捕获异常 |
| G85 | §2.1 补单实例锁（requestSingleInstanceLock）：双 GUI 抢同一会话 UI 竞争 + daemon spawn 竞态 |
| G86 | §2.1 补菜单与 DevTools：保留默认编辑菜单（macOS 剪贴板快捷键依赖）；生产禁 DevTools；菜单精简方案 |
| G87 | §2.1 补窗口状态持久化（bounds 存 ~/.emrg/gui-window.json，v1 低成本项） |
| G88 | §3.4 补 auth 失败语义：auth_ok 前 ws close = 认证失败（对照 connect.py AuthError），停止自动重试 + 手动重试——防无限重连循环 |
| G89 | §3.4 补断连时进行中流：标记「连接中断」+ 清流式状态 + 恢复输入条；v1 不自动恢复（G12 无历史），用户重发 |
| G90 | §6.1 修正 isRunning 单测为 TCP 探测（G28 描述随 G43 过时）+ 补 auth 失败/断连 pending 请求单测 |
| G91 | §5.3 补大内容保护：tool_end content 默认截断 2000 字符 + 展开；消息 DOM 上限 200 条（v1 简单策略） |
| G92 | §5.3 补 backgroundThrottling: false——后台窗口 rAF 节流会导致流式延迟 |
| G93 | 新增 §4.4 命令-响应配对机制：pending 命令队列（type 配对 + FIFO + 5s 超时）；sessions_list 无 request_id 须靠顺序配对；广播帧不占 pending；断连全 reject |
| G94 | §3.3 补流结束兜底：最后帧后 30s 无 done 强制结束流式（wrapper 未捕获异常/进程崩溃无 done 帧） |
| G95 | §3.3 补协议健壮性：pong 5s 超时视为连接异常；旧 daemon 返回 unknown message type → pending reject + 版本提示；unknown 帧 log 丢弃 |
| G96 | §3.1 sendTask 补 stream:true 必须显式进 payload（daemon 读 stream 默认 False，daemon.py:326——漏发则走非流式、GUI 收不到 delta）；payload 对照 TaskRequest.to_dict 全量字段 |
| G97 | §3.4 补断连时工具卡片清理：tool_start 已建/tool_end 未到 → 标「结果未知——连接中断」；广播分组/pending 队列/自有流缓存全清；工具无重试语义（避免重复副作用） |
| G98 | §4.2 saveSettings 补重连后刷新模型：daemon 重启后补 list_models 拿新 current 更新状态栏（否则显示旧 model） |
| G99 | §6.1/6.2 测试路径定案 `emrg/gui/test/`（与 npm 工作目录一致，原 tests/gui/ 会让 node:test 找不到）+ 框架定案 node:test（零依赖，去 vitest 二选一含糊） |
| G100 | §6.4 补 CI 细节：working-directory emrg/gui；集成测试依赖 .venv（uv sync 复用，G59 同款定位）；v1 建议单测必跑 + 集成本地跑（EMRG_SKIP_INTEGRATION 开关） |
| G101 | §2.1 补 renderer 崩溃恢复：render-process-gone → 提示 + 重新 loadFile（main 持有窗口/daemon_client/IPC 不受影响）；unresponsive → 可重新加载 |
| G102 | §2.1 补 IPC 输入校验（纵深防御）：sessionId 正则、text ≤ 20000 字符、config 三键白名单——渲染层被攻破后不能乱调 IPC |
| G103 | §4.4 补 error 帧配对：error 帧无 type（daemon.py:306/314/976）——pending 表按 type 配对会漏；有未决命令时 FIFO reject 最近的 pending，无则广播 renderer |
| G104 | §3.3 补分组在 tool_start 时也创建（LLM 先出 tool_calls 后出文本）；工具卡片按 request_id 归属分组节点；无 request_id 的 tool 帧 → 当前分组或丢弃+warning |
| G105 | §3.2 maxPayload 更正：**维持 16MB**（工具输出有上限：bash 200KB/read 256KB/grep 200 条，bash_tool.py:16——tool_end 不会超 16MB；原"放宽 64MB"是过度设计）；工具输出截断事实补入 G91 |
| G106 | §5.2 补被动删除恢复：delete_session 非广播（daemon.py:866-897），他端删除 GUI 残留——resume_result error 时自动 list_sessions 刷新 + 切换最近会话 |
| G107 | §6.1 补命令-响应配对单测（G93/G103：resolve/超时/error FIFO）与分组生命周期单测（G83/G104：tool_start 建组/done 清理/超时/上限）——核心机制此前无测试条目 |
| G108 | §3.2 补 server_id 格式：{instance_id[:8]} @ {host_name}（对照 TUI app.py:326-328，pong identity daemon.py:616-617）；model 读 pong 的 model 字段（daemon.py:624） |
| G109 | §3.2 补窗口标题同步：切会话时 setTitle「EMRG — {显示名}」（对齐 TUI 终端标题同步，G27 兜底） |
| G110 | §5.4 补会话切换时广播分组隔离：switchSession 清空旧 session 分组缓存（含 timer）；DOM 节点保留只清缓存（G83 语义） |
| G111 | §5.2 补 model 名校验：设置对话框 model 用下拉只列 list_models 合法模型（daemon.py:1905-1915 任意名保留旧 context_window）；不提供自由文本输入 |
| G112 | 一致性修复：① §4.2 newSession 参数改 cwd 由 main 注入（原 {cwd} 与 G22/preload 无参矛盾）；② init 返回补 project_dir（G6 已要求显示状态栏项目名）；③ SettingsDialog 补 project_dir 字段（G82"可在设置对话框改"缺 UI 入口，复用 pickProjectDir） |
| G113 | §6.3 E2E 冒烟补三项：设置保存→重启→model 刷新（G98 验证）；广播流+会话切换→分组清理（G110）；大工具输出→截断显示+不断连（G91/G105） |
| G114 | §2.1 一致性修复：G102 config 白名单补 projectDir（G112 后 SettingsDialog 4 字段，三键白名单会剥掉 project_dir 导致保存丢目录） |
| G115 | §5.2 补字段名映射：renderer projectDir（camelCase）→ main 落盘 [gui] project_dir（snake_case），映射只在 main 一次；pickProjectDir 只选不写盘，保存才落盘（取消不丢旧值） |
| G116 | §5.2 首启保存优先复用 ensure_config()（client/__main__.py:12 同款）生成官方模板（含 [[llm.models]] 预置）再改字段——自研写最小文件会丢预置模型，G111 model 下拉只剩一项 |
| G117 | §5.2 补占位符坑：模板 api_key="sk-..."（config.py:108）——"是否已配 key"判断必须排除占位符，否则首启引导被跳过且 key 无效 |
| G118 | §4.2 getSettings 返回形状定案：camelCase {apiKey, baseUrl, model, projectDir, models}；api_key 明文返回 renderer（沙箱可读，写入侧 G102 纵深防御）；占位符 sk-... 返回空串（G117） |
| G119 | §4.2 saveSettings 补重启期间状态：pending 全 reject（G93-4）、流式任务被自杀打断（G89 语义）、UI 提示层级、**保存后不主动重连**（等 ws close 自然触发，避免连上"正在关闭"的旧 daemon） |
| G120 | §2.1 澄清单实例锁动机：daemon 自身有 pid 文件原子互斥（daemon.py:130-141）——双 spawn 不会起两个 daemon（第二个自我退出）；G85 ② 实为 UI 竞争（①）而非 daemon 重复；GUI+TUI 同开无竞态 |
| G121 | §5.2 补 project_dir 失效处理：daemon 对任意 cwd 自动 mkdir（session.py:55）——init 校验 project_dir 存在可写，失效则禁用发送+弹设置引导；不自动 fallback（显式优于静默换目录） |
| G122 | §3.3/§5.3 补 IPC 批量推送：main 对 message_delta 16ms 批量（一条 event 带 chunks），tool_*/done/error 不批量；renderer rAF 在其上再合并——长回复 IPC 消息量降 ~60 倍 |
| G123 | §5.2 澄清首启保存 vs 常规保存重连差异：G119"不主动重连"仅适用常规保存（daemon 已运行）；首启保存（config 从无到有）daemon 未运行、无 ws 可 close——saveSettings 返回 daemonWasRunning，false 则主动 init 拉起（否则永久卡首启） |
| G124 | §3.1 补"当前发起流"缓存清理时机：done + G94 超时 + G89/G119 断连 + 下次 sendTask 前置——统一 clearActiveStream()，防旧 request_id 误配广播帧/残留 pending |
| G125 | §3.2 startDaemon 补 cwd=project_dir：daemon 的 load_skills() 用 Path.cwd()（daemon.py:120）加载项目级 skills——GUI 从任意目录启动必须显式设 cwd（TUI 从项目目录启动天然正确），否则项目 skills 加载不到 |
| G126 | §6.2 补 pythonPath 来源（G59/G61 同款定位，集成测试直接 spawn 已知路径）+ skills 目录缺失容忍实证（loader.py:112 is_dir 判断，隔离环境无需预建） |
| G127 | §5.3 补 done 后渲染不阻塞：长回复（几十 KB）同步 marked 渲染阻塞主线程 100-500ms——requestIdleCallback 调度；rIC 不可用则直接同步（G91 200KB 上限兜底） |
| G128 | §3.3 补 session busy error 帧带 session_id（daemon.py:315）——UI 据此定位具体会话显示"正忙"，多会话不误导 |
| G129 | §3.4 补无协议版本协商现状：auth_ok 纯 {"type":"auth_ok"}（daemon.py:250）无版本号——兼容靠 G95 兜底；不实现 auth 带版本（零改动原则） |
| G130 | §5.2 澄清 title 不自动出现：_generate_session_title 只在 rename_session 空 title 时被调（daemon.py:741），TUI 仅 /rename 触发（app.py:1335）——新会话 title 恒 session_id 兜底，GUI v1 不自动生成 |
| G131 | §5.3 补 IPC 传输全量 + DOM 只放截断：tool_end 全量 content 经 IPC（200KB 可接受）、renderer 存全量但 DOM 插 2000 字符、展开用 textContent 纯文本（不做 marked）；main 侧不截断 |
| G132 | §5.3 补 highlight.js 语言子集：只注册 ~20 常用语言（全量 190+ 增包体积 ~1MB），未注册降级纯文本；highlightAuto 慎用（短代码误判），优先精确 lang |
