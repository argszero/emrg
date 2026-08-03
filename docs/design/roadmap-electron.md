# EMRG 未来架构路线图（Electron 修订版）：WebSocket 协议 · Electron GUI · 安装文件分发

> 本文是 EMRG 未来演进的主路线图，**基于 GUI 采用 Electron 的决策**修订。
> 原路线图（PySide6 前提）见 git 历史；本版推翻 PySide6 选型，改为 Electron + Node.js。
>
> 关联文档：
> - 协议细节 → [`phase1-websocket-protocol.md`](phase1-websocket-protocol.md)（已完成，435 测试全绿）
> - 打包细节 → [`packaged-installer.md`](packaged-installer.md) §1-6（仍适用，GUI 入口变 Electron）
> - GUI 详细设计 → 本文 §5（新写，替代 packaged-installer.md §7/§7.1 的 PySide6 设计）

---

## 1. 架构决策：为什么 Electron

**决策**：GUI 用 Electron（Node.js + Chromium），不用 PySide6。

| 维度 | PySide6（原方案） | Electron（新方案） |
|------|-------------------|-------------------|
| 语言 | Python（单一栈） | **JS/TS + Python daemon（双栈）** |
| UI 能力 | Qt Widgets（受限） | **HTML/CSS/JS（现代 UI，生态庞大）** |
| 体积 | ~80-120MB | ~100-200MB（Chromium） |
| 非开发者吸引力 | 一般 | **强**（品牌、动效、Web 资产复用） |
| daemon 交互 | import daemon_manager | **WS 协议 + spawn CLI** |
| 打包 | PyInstaller（Python 栈） | electron-builder（Node 栈） |
| 内存占用 | 低 | 高（~200MB+） |

**核心影响**：GUI 不再复用 Python 代码——它通过 **WebSocket 协议**（Phase 1 已完成）与 daemon 通信，daemon 是唯一生命本体。这反而**强化了协议的价值**：协议是 TUI、GUI、未来任何客户端的**唯一契约**。

**架构不变的部分**（Electron 不触碰）：
- daemon（生命本体）——Python，零改动
- 协议（WebSocket + JSON + token 认证）——Phase 1 已完成，GUI 用 JS 实现同一协议客户端
- TUI——Python，继续演进，与 GUI 平等

---

## 2. 依赖分析：Electron 前提下的顺序

```
协议 WebSocket ──► 协议契约固化 ──► Electron GUI ──► 安装打包 ──► 远程连接
  (Phase 1 ✅)       (Phase 2)        (Phase 3)     (Phase 4)     (Phase 5)
```

**排序的五条理由（修订）**：

| # | 原则 | 说明（Electron 修订） |
|---|------|----------------------|
| 1 | **依赖驱动** | GUI 依赖协议契约（JS 实现 WS 客户端）；打包依赖 GUI 就绪 |
| 2 | **风险前置** | 协议改造已完成（Phase 1）；Phase 2 把协议契约固化，是 JS 客户端的地基 |
| 3 | **复用优先** | **复用对象从"代码"变为"契约"**——TUI 与 GUI 共享协议规范，而非共享代码（JS 无法 import Python） |
| 4 | **完整优先** | 安装包在功能齐备后一次打全（TUI + GUI + daemon 三入口） |
| 5 | **增强置后** | 远程连接是 WebSocket 的自然延伸，放最后 |

> **Phase 2 的重新定位**：原 Phase 2（daemon_manager 提取）是为"Python GUI 复用代码"设计的。Electron 下，daemon_manager 的**代码**价值下降，但其**副产品——协议事件流契约（read_stream 的事件类型）**成为 JS 客户端的直接依据。因此 Phase 2 从"提取 Python 共享层"改为"**固化协议契约文档 + TUI 瘦身**"。

---

## 3. 分阶段实现

### Phase 1 — 协议 WebSocket 化（✅ 已完成）

**状态**：已完成（435 测试全绿，运行时已切换）。`framing.py` 删除，TUI/daemon/CLI 全走 WS。

---

### Phase 2 — 协议定型（契约固化 + 广播模型 + TUI 瘦身，当前阶段）

**目标**：把协议从"代码里的事实"固化为**终态契约**——Electron 端照文档用 JS 实现，不需读 Python 源码。协议一次定型到广播模型（多客户端一致），避免"契约写现状、GUI 前再改目标"的撕裂。

**内容**：

```
docs/design/protocol-contract.md    # 协议契约（JS 客户端的唯一依据）✅ 初稿完成（11 轮 review）
emrg/server/daemon.py               # 广播模型实施（订阅表 + session 锁 + 流式广播）
emrg/client/daemon_manager.py       # 新：TUI 瘦身提取（可选，纯 TUI 维护性）
emrg/client/app.py                  # 瘦身（1994 → <1200 行）
tests/test_ws_e2e.py                # 补广播用例
```

**协议契约文档必须包含**（Electron 端唯一需要的东西）：

| 项 | 内容 |
|----|------|
| 连接 | port 文件读取、`ws://127.0.0.1:<port>`、auth 首帧、auth_ok、max_size 16MB |
| 消息类型 | 客户端→daemon：`ping/task/cancel/compact/list_sessions/resume_session/rant/…`（全量 17 种） |
| 流式事件 | 服务端→客户端：`delta/tool_start/tool_end/done/error` 的**逐字段格式**（含广播语义） |
| 错误语义 | 认证失败（AuthError）、断线（ConnectionClosed）、非 dict JSON、超时 |
| 会话语义 | session_id 传递、断连记忆整合、shutdown 流程 |

**广播模型**（架构决策，2026-08-03）——同一 session 的所有客户端看到相同响应：用户从多个客户端打开同一 session = 从不同地方和同一 Agent 对话，看到同样结果。

- daemon 维护 `session_subscribers: dict[session_id, set[ws]]`——连接发带 session_id 的消息即订阅；断开退订
- task 流式响应广播给 session 的所有订阅者（含发起者）
- **session 级锁**：同一 session 同时只允许一个 task（第二个返回 "session busy"）——同时解决多连接写竞态（compact 全量重写 vs append 并发丢消息）
- 契约文档同步更新为广播语义（§2.2/§3.1/§5，消除"现状+目标"两卷混合）

**验收**：
- [ ] 协议契约文档与 daemon 实际行为一致（逐消息核对）——契约描述**终态协议**，无"现状 vs 目标"杂质
- [ ] 两连接同 session：A 发 task，B 实时看到相同流式响应
- [ ] session 级锁：A 的 task 未完成时 B 发 task 收到 "session busy"
- [ ] 断开退订：A 断开后不再收到广播
- [ ] 写竞态消除：compact 与 append 不再并发丢失
- [ ] Electron 端按文档实现的最小 JS WS 客户端能完成 ping/流式聊天（**契约可执行性验证**）
- [ ] TUI 瘦身到 <1200 行（可选，不影响契约）

---

### Phase 3 — Electron GUI（极简 v1）

**目标**：`emrg-gui` 可用，覆盖 80% 日常（聊天/会话/工具状态/设置）。

**技术栈**：
```
Electron + TypeScript
├── main process      # 启动/探测 daemon（spawn `emrg server` CLI）、WS 客户端、窗口管理
├── renderer          # HTML/CSS/React（或 Svelte）——UI 只渲染，零状态
└── preload           # contextBridge 暴露安全的 IPC 桥（renderer 不直接碰 Node）
```

**代码结构**（`gui/`，独立于 Python 包）：

```
gui/
├── package.json
├── src/
│   ├── main/            # Electron 主进程
│   │   ├── daemon.ts    # 探测 port 文件、spawn/重启 daemon（调 emrg CLI）
│   │   ├── ws-client.ts # 按协议契约实现 WS 客户端（auth 首帧/流式事件/重连）
│   │   └── main.ts      # 窗口生命周期
│   ├── preload/
│   │   └── bridge.ts    # contextBridge: sendMessage / onEvent / daemonStatus
│   ├── renderer/
│   │   ├── App.tsx      # 聊天视图 / 会话列表 / 工具状态 / 设置对话框
│   │   └── …
└── build/               # electron-builder 配置（mac .app / win .exe / linux AppImage）
```

**关键设计**：
- **daemon 交互**：主进程读 `~/.emrg/emrgd.port` → 不存在则 `spawn('emrg', ['server', 'restart'])` → 轮询 port 文件 → WS 连接。**不 import Python，只 spawn CLI + 读 port 文件**（Electron 无法 import Python，这是唯一路径）
- **WS 客户端**：Node 内置 WebSocket（Node 22+）或 `ws` 库——按协议契约实现 auth 首帧 + auth_ok + 流式事件 + 断线重连（语义对齐 TUI 的 `_reconnect`）
- **renderer 零状态**：所有状态在 main process，renderer 只通过 preload 桥收发消息（安全，对齐 Electron 最佳实践）
- **共享唯一 daemon**：GUI 与 TUI 同时连同一 daemon（Phase 1 已验证多客户端并发）

**验收**：
- [ ] 全新环境：装好即开 GUI → 首启引导填 key → 聊天 → 工具状态显示
- [ ] 流式不卡 UI（main/renderer 分离天然不卡）
- [ ] daemon 被杀自动重连（对齐 TUI `_reconnect` 语义）
- [ ] TUI + GUI 同开，会话/记忆一致
- [ ] **协议契约可执行性**：GUI 的 ws-client.ts 是契约文档的活验证（发现契约遗漏即回补文档）

---

### Phase 4 — 安装文件分发（Electron 版）

**目标**：`.pkg` / `.exe` / `.AppImage` 一键安装，安装即完整，卸载彻底。

**变化**（相对原 PySide6 版）：
- **Python 侧**：PyInstaller 打包 `emrg`（TUI）+ `emrgd`（daemon）——两个入口
- **Electron 侧**：electron-builder 打包 `emrg-gui`（含 Chromium，~100-200MB）
- **安装器**：macOS `.pkg` 同时装 Python 双入口 + GUI app；Windows `.exe`（NSIS）；Linux AppImage
- **git/gh 捆绑**：不变（daemon 演化需要）

**可与 Phase 3 并行准备**：electron-builder 配置、CI matrix、签名流程。

**验收**：
- [ ] 干净机器双击安装 → TUI + GUI + daemon 全可用
- [ ] `emrg uninstall` 清理彻底（含 GUI app 与 Python 入口）

---

### Phase 5 — 远程连接（WebSocket 延伸）

**目标**：客户端在笔记本、daemon 在服务器。协议已统一，加加密/认证。

**变化（Electron 是天然的远程客户端）**：
- GUI 主进程的 `ws-client.ts` 支持 `wss://<host>:<port>` + token 配置（UI 填）
- `wss://`（TLS）+ token + 自签名证书 TOFU 指纹验证（无 CA，SSH known_hosts 模式）
- 远程不自动拉起 daemon；工具执行/会话/记忆全在远端（GUI 是瘦终端）

**验收**：
- [ ] 远端全功能：聊天 + 工具 + 会话（工具执行在远端）
- [ ] 安全验收：无 TLS 拒绝启动、token 错拒连、指纹变更拒连

---

## 4. 里程碑总览

| Phase | 内容 | 依赖 | 风险 | 产物 |
|-------|------|------|------|------|
| 1 ✅ | 协议 WebSocket 化 | — | 高（已消解） | 统一协议（framing.py 删除，435 测试） |
| 2 | **协议定型**（契约固化 + 广播模型 + TUI 瘦身） | 1 | 中（daemon 改造） | protocol-contract.md（终态契约）+ 广播 daemon + session 锁 |
| 3 | Electron GUI v1 | 1+2 | 中（双栈、新工程） | `emrg-gui`（.app/.exe/AppImage） |
| 4 | 安装文件分发 | 1+2+3 | 中（打包坑多） | 三入口安装包 |
| 5 | 远程连接 | 1（协议已统一） | 中（安全敏感） | wss + TOFU + 认证 |

**并行建议**：Phase 3 与 Phase 4 的 electron-builder/CI/签名可并行；Phase 2 的契约文档应在 Phase 3 开始前完成（GUI 的地基）。

---

## 5. 决策记录（Electron 修订）

1. **GUI 技术栈：Electron（推翻 PySide6）**——现代 UI 能力、Web 生态、非开发者吸引力，代价是双栈 + 体积 + 内存。
2. **复用对象从"代码"变为"契约"**——TUI（Python）与 GUI（JS）共享协议契约文档，而非共享代码；协议是唯一契约。
3. **Phase 2 重定位**：从"daemon_manager 提取（为 Python GUI）"改为"协议契约固化 + TUI 瘦身"——契约文档是 JS 客户端的地基。
4. **GUI 与 daemon 交互走 spawn CLI + 读 port 文件**——Electron 主进程不 import Python，只 spawn `emrg server` + 读 `~/.emrg/emrgd.port`。
5. **打包双轨**：PyInstaller（Python 入口）+ electron-builder（GUI），安装器合一。
6. **远程是增强项，最后做**——Electron 是天然远程客户端（Node WebSocket 成熟）。
7. **多客户端广播模型（新决策，并入 Phase 2）**：同一 session 的所有客户端看到相同响应——用户从多个客户端打开同一 session = 从不同地方和同一 Agent 对话。实现：session 订阅表 + 流式广播 + session 级锁。这同时解决多连接写竞态。作为 Phase 2（协议定型）的一部分实施，不单独设阶段。
