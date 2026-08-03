# EMRG 未来架构路线图：WebSocket 协议 · GUI 客户端 · 安装文件分发

> 本文是 EMRG 未来演进的主路线图，定义**三个目标**与**实现顺序**。
> 细节设计分散在既有文档，本文只做整合与排序：
> - 安装/分发/卸载细节 → [`packaged-installer.md`](packaged-installer.md) §1-6
> - GUI 详细设计 → [`packaged-installer.md`](packaged-installer.md) §7 / §7.1
> - 远程连接细节 → [`packaged-installer.md`](packaged-installer.md) §8
> - **协议 WebSocket 化（Phase 1）** → [`phase1-websocket-protocol.md`](phase1-websocket-protocol.md)（详细设计）
>
> **Phase 编号说明**：`packaged-installer.md` 的 Phase 1-6 是**打包实施的子计划**（spec/CI/签名/卸载器…）；本文 Phase 1-5 是**架构演进主线**（协议→共享层→GUI→打包→远程）。两条线的关系：本文 Phase 4（安装分发）内部再按 packaged-installer 的 Phase 1-6 细化推进。读者以本文为主线，packaged-installer 为执行细节。

---

## 1. 现状与三个目标

### 现状

| 项 | 实现 |
|----|------|
| 客户端 | TUI（`emrg/client/app.py`，1997 行，与 daemon 管理、协议读写、渲染揉在一起） |
| 服务端 | daemon（`emrg/server/daemon.py`，2245 行，生命本体） |
| 协议 | JSON 消息（`protocol.py`）+ 4 字节长度帧（`framing.py`）+ UDS/Named Pipe（`connect.py`） |
| 依赖 | rich / httpx / pyyaml / jinja2（无网络协议库） |
| 测试 | 429 项全绿 |
| 安装 | `install.sh`（curl \| bash，将废弃） |

### 三个目标

1. **协议 WebSocket 化**：统一 client↔server 通信协议为 WebSocket，JSON 消息层不变，本机/远程共用一套协议。
2. **GUI 客户端**：`emrg-gui`，与 TUI 平等的入口，共享唯一 daemon，零状态只渲染。
3. **安装文件分发**：`.pkg` / `.exe` / `.AppImage`，捆绑 Python + git + gh，安装即完整，卸载彻底。

---

## 2. 依赖分析：为什么是这个顺序

```
协议 WebSocket ──► 共享客户端层 ──► GUI ──► 安装打包 ──► 远程连接
  (Phase 1)         (Phase 2)      (3)      (4)          (5)
      ▲                ▲            ▲        ▲            ▲
   最底层改动           GUI 的地基   依赖 1+2  依赖全部功能  协议已统一，
   风险最高             协议已稳     纯增量    一次打全      纯增强
```

**排序的五条理由**：

| # | 原则 | 说明 |
|---|------|------|
| 1 | **依赖驱动** | 每阶段的产物是下一阶段的输入。GUI 依赖协议与共享层；打包依赖全部功能就绪 |
| 2 | **风险前置** | 协议改造动核心通信（TUI + server 双方），是全程最高风险——最先做，此时无 GUI/打包叠加，失败成本最低 |
| 3 | **复用优先** | 共享客户端层在 GUI 之前提取，GUI 直接复用，杜绝"GUI 复制网络代码" |
| 4 | **完整优先** | 安装包在功能齐备后一次打全（TUI + GUI + daemon 三入口），避免"先打包 TUI、GUI 出来再重新打包"的返工 |
| 5 | **增强置后** | 远程连接是 WebSocket 协议的自然延伸，是锦上添花——不阻塞主线，放最后 |

> **不推荐的顺序**：先做 GUI（协议没稳，GUI 基于错误地基返工）、先打包（功能没齐，产物残缺）、先远程（协议未统一，远程要写两套协议）。

---

## 3. 分阶段实现

### Phase 1 — 协议 WebSocket 化（风险最高，最先做）

**目标**：client↔server 通信统一为 WebSocket，JSON 消息不变，本机行为无回退。

**设计**：

```
┌─────────────────────────────────────────────┐
│                 emrgd                        │
│   websockets.serve(handler, 127.0.0.1, 0)    │
│     本机:  ws://127.0.0.1:<动态端口> + token │
│     远程:  wss://0.0.0.0:8743 (TLS, Phase 5) │
└─────────────────────────────────────────────┘
        ▲ ws://127.0.0.1（本机） / wss://（远程，Phase 5）
        │
┌───────┴────────┐  ┌──────────┐
│ TUI (emrg)     │  │ GUI       │ ← Phase 3 直接复用同一协议
│ websockets     │  │ websockets│
└────────────────┘  └──────────┘
```

| 层 | 现状 | 改造后 |
|----|------|--------|
| 消息 | `protocol.py` JSON | **不变**（`TaskRequest`/`ToolStart`/… 序列化逻辑原样） |
| 帧 | `framing.py` 4 字节长度前缀 | **删除**——业务直接用 WS 原生 `ws.recv()`/`ws.send()`，一条 WS 消息 = 一个 JSON 帧（详见 [`phase1-websocket-protocol.md`](phase1-websocket-protocol.md) §3） |
| 传输 | `connect.py` UDS/Named Pipe 平台分发 | **websockets 统一 TCP loopback**：本机 `ws://127.0.0.1:<port>`（动态端口 + token 首帧，无平台分叉），远程 `wss://` |
| 健康探测 | `is_server_running_sync()` socket connect | TCP connect 到 port 文件中的端口 |

**要点**：
- 引入 `websockets` 库（asyncio 原生，成熟稳定）。这是项目第一个网络协议依赖——但它是三个目标的共同地基，值得。
- **本机统一用 `ws://127.0.0.1:<port>` 而非 `ws+unix://`**：协议统一意味着只维护一套传输逻辑——本机/远程差异收敛为"要不要 TLS + token"；loopback 无端口冲突（动态端口），安全边界由本机 token（存 600 权限文件）兜底，与远程共用同一认证机制。详见 [`phase1-websocket-protocol.md`](phase1-websocket-protocol.md) §3.1。
- 服务端 handler 逻辑（消息分发、工具循环、会话管理）**零改动**——只换监听方式。
- 客户端 daemon 生命周期逻辑（拉起/重启/重连）**零改动**——只换连接方式。
- 远程（Phase 5）时本机路径天然不变，只需加 `wss://` 监听 + TLS + 认证。

**验收**：
- [ ] 429 项既有测试全绿（协议相关测试改写为 WebSocket 断言）
- [ ] 本机全功能无回退：聊天流式 / 工具调用 / 会话持久化 / `/rant` / 演化周期
- [ ] 大消息（>1MB 工具输出）无截断（验证 WS 消息无 64KB 限制问题）
- [ ] ESC 中断、cancel 语义与现状一致

**风险**：websockets 库行为差异（流式多帧、连接关闭语义）→ Phase 1 冒烟测试重点覆盖（详见 [`phase1-websocket-protocol.md`](phase1-websocket-protocol.md) §8）。

---

### Phase 2 — 共享客户端层提取（daemon_manager）

**目标**：从 `client/app.py`（1997 行）提取 daemon 管理 + 协议客户端封装，TUI 瘦身，GUI 白拿。

**内容**：

```
emrg/client/
├── daemon_manager.py   # 新提取：start_server_daemon / check_and_restart_if_stale /
│                       #   connect / read_stream（yield delta|tool_start|tool_end|done）
└── app.py              # 瘦身：只留 TUI 渲染与交互，网络逻辑全部走 daemon_manager
```

**验收**：
- [ ] TUI 全功能回归（全量 pytest + 手动冒烟）
- [ ] `daemon_manager` 独立单测覆盖（不依赖 TUI 渲染）
- [ ] 提取后 `app.py` 行数显著下降（目标 <1200 行）

**为什么在 GUI 前**：GUI 不复制网络代码是核心原则（§7.1 复用层）。协议在 Phase 1 已稳，此时提取无后顾之忧。

---

### Phase 3 — GUI 客户端（极简 v1）

**目标**：`emrg-gui` 可用，覆盖 80% 日常（聊天/会话/工具状态/设置）。

**设计**（详见 `packaged-installer.md` §7.1）：
- PySide6（仅 QtWidgets）+ qasync 单线程
- `emrg/gui/`：main_window / daemon_client（信号桥）/ chat_view / session_panel / settings_dialog
- 复用 `daemon_manager`（Phase 2），不复制网络代码
- 与 TUI 平等入口，共享唯一 daemon

**验收**：
- [ ] 全新环境：装好即开 GUI → 首启引导填 key → 聊天 → 工具状态显示
- [ ] 流式不卡 UI；daemon 被杀自动重连
- [ ] TUI + GUI 同开，会话/记忆一致

---

### Phase 4 — 安装文件分发

**目标**：`.pkg` / `.exe` / `.AppImage` 一键安装，安装即完整，卸载彻底。

**设计**（详见 `packaged-installer.md` §1-6）：
- PyInstaller 三入口（`emrg` / `emrgd` / `emrg-gui`）+ git/gh 捆绑
- 用户级安装（免 sudo）；统一安装方式（不再区分开发者/非开发者）
- 卸载：终止报告 + 墓地快照 + 清理 + 自校验

**可与 Phase 3 并行准备**：spec 文件、CI matrix、签名流程可以在 GUI 开发期间搭好，GUI 一合入即出完整安装包。

**验收**：
- [ ] 干净机器（无 python/uv/git）双击安装 → 全部功能可用（TUI + GUI + 演化）
- [ ] `emrg uninstall` 清理彻底，无残留

---

### Phase 5 — 远程连接（WebSocket 的延伸，增强项）

**目标**：客户端在笔记本、daemon 在服务器。协议已统一（Phase 1），只需加加密/认证。

**设计**（详见 `packaged-installer.md` §8）：
- `wss://`（TLS）+ token + 自签名证书 TOFU 指纹验证（**无 CA**，SSH known_hosts 模式）
- SSH 隧道为官方零代码路径（隧道内 ws 明文——加密分层，不叠加）
- 远程不自动拉起/不重启 daemon；工具执行/会话/记忆全在远端（客户端是瘦终端）

**验收**：
- [ ] 远端全功能：聊天 + 工具 + 会话（工具执行在远端）
- [ ] 安全验收：无 TLS 拒绝启动、token 错拒连、指纹变更拒连、抓包无明文

---

## 4. 里程碑总览

| Phase | 内容 | 依赖 | 风险 | 产物 |
|-------|------|------|------|------|
| 1 | 协议 WebSocket 化 | — | 高（动核心通信） | 统一协议，业务改用 WS 原生 API（framing.py 删除） |
| 2 | daemon_manager 提取 | 1 | 中（TUI 回归） | 共享客户端层 |
| 3 | GUI v1 | 1+2 | 中（新栈） | `emrg-gui` |
| 4 | 安装文件分发 | 1+2+3 | 中（打包坑多） | .pkg/.exe/.AppImage |
| 5 | 远程连接 | 1（协议已统一） | 中（安全敏感） | wss + TOFU + 认证 |

**并行建议**：Phase 3 与 Phase 4 的准备期（spec/CI/签名）可并行；Phase 2 可在 Phase 1 后半段同步开始（协议稳定后）。

---

## 5. 决策记录（本路线图新增/修订）

1. **协议统一为 WebSocket**：本机 `ws://127.0.0.1:<port>`（动态端口 + 本机 token）、远程 `wss://`；JSON 消息层不变，业务改用 WS 原生 API；`framing.py` 删除。
2. **实现顺序**：协议 → 共享层 → GUI → 打包 → 远程（依赖驱动 + 风险前置 + 复用优先 + 完整优先 + 增强置后）。
3. **`websockets` 是三个目标的共同地基依赖**：接受其作为项目第一个网络协议库。
4. **打包在功能齐备后一次打全**：不先打 TUI 再补 GUI（避免返工）。
5. **远程是增强项，最后做**：协议统一后远程只是加加密/认证层，不阻塞主线。
