# 🧱 EMRG

<p align="center">
  <strong>一个会写代码的 AI 编程助手——而且它还会<em>重写自己</em>。</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
  <img alt="Status" src="https://img.shields.io/badge/status-evolving-orange.svg">
  <img alt="Tests" src="https://github.com/argszero/emrg/actions/workflows/test.yml/badge.svg">
  <img alt="PRs Welcome" src="https://img.shields.io/badge/PRs-by%20AI%20%2B%20human-brightgreen.svg">
</p>

<p align="center">
  <a href="README.en.md">🇬🇧 English</a> | <b>🇨🇳 中文</b>
</p>

---

**如果每次使用，你的编程助手都在变得更好，会怎样？**

EMRG 是一个关于*自主进化*的实验。它能帮你写代码——读文件、跑命令、做编辑——但核心差异在于：**你的每一条吐槽 (`/rant`) 都会驱动它自我改进**。你说它哪里不好，下一个演化周期它就自己写代码改。配合 GitHub 社区动态和竞品更新，EMRG 在后台持续进化，越用越顺手。完全开源，完全透明。

> *"EMRG 是一个自我进化的 AI 智能体架构实验。"* — [MANIFESTO](MANIFESTO.md)

---

## ✨ 为什么你会爱上它

| 特性 | 说明 |
|---|---|
| 🧠 **读写改跑，样样精通** | 完整的工具调用能力——bash、文件读写、diff 编辑，全在终端里搞定 |
| 🔄 **吐槽驱动进化** | 你的 `/rant` 直接驱动后台演化循环——吐槽 → 分析 → 写代码 → 提 PR → 自动变强 |
| 📝 **永不忘事** | 项目记忆 + 会话记忆 + 每日日志——上下文持续保留，不怕断线 |
| 🖥️ **颜值在线的 TUI** | 斜杠命令自动补全、会话选择器、流式 Markdown 渲染、请求计时器、ESC 中断 |
| 🖱️ **Electron GUI（新）** | 非开发者主入口——聊天/会话/工具状态/设置，自动拉起 daemon，`npm start` 即开即用。v0.2.5 完全重设计：浅/深双主题跟随系统、工具过程友好化（折叠展开）、多模型管理（增删改/设默认/对话中切换）、空状态欢迎屏、回到底部按钮。v0.2.6 键盘可达性补全：模型切换器/右键菜单/对话列表 ↑↓ 导航、表单 Enter 提交（全组件键盘可用）。v0.2.7 macOS 签名+公证：Developer ID 双证书、内嵌 Python runtime 签名、公证+spctl 终验——安装零 Gatekeeper 弹窗 |
| ⚡ **并行工具调用** | 独立的工具调用并发执行，速度飞快 |
| 🔌 **微内核守护进程** | `emrgd` 持久运行——随时重连，状态不丢 |
| 🎮 **Vim 友好** | `j`/`k` 导航、`Ctrl+W`/`Ctrl+K` 编辑、`Tab` 展开工具卡片 |
| 🌍 **100% 开源** | MIT 协议——没有围墙，没有厂商锁定 |

---

## 🚀 快速开始

### 📦 下载安装包（推荐，Phase 4 一键安装）

到 [GitHub Releases](https://github.com/argszero/emrg/releases) 下载对应平台的安装文件，双击安装即可：

| 平台 | 安装文件 | 说明 |
|------|---------|------|
| macOS (Apple Silicon) | `EMRG-<ver>-macos-arm64.pkg` | 双击安装（用户级，无需管理员密码），GUI 到 `~/Applications/EMRG.app` |
| macOS (Intel) | `EMRG-<ver>-macos-x64.pkg` | 同上 |
| Windows | `EMRG-<ver>-windows-x64.exe` | Inno Setup 免 UAC，开始菜单快捷方式，PATH 自动注册（含原生 TUI，cmd/PowerShell 直接 `emrg`） |
| Linux | `EMRG-<ver>-linux-x86_64.AppImage` | 首次运行自解压到 `~/.emrg/install/` |
| Linux (ARM64) | `EMRG-<ver>-linux-aarch64.AppImage` | 同上 |

安装包内置完整运行时（standalone Python 3.13 + 依赖 + git + gh + GUI），**干净机器（无 python/uv/git/gh/node）零前置依赖**，100% 离线安装。安装后：

- GUI：启动台 / 开始菜单点击 **EMRG**
- TUI：`~/.emrg/install/bin/emrg`（macOS 安装时写 shell rc PATH anchor，新开终端直接 `emrg`；Linux 另生成 `~/.local/bin/emrg` 软链）
- 首次启动引导填写 API key；会话内可直接执行 python 脚本

> **卸载**：macOS 运行"卸载 EMRG.app"；Windows 控制面板卸载；Linux 运行 `~/.emrg/install/bin/emrg-uninstall`（或删 AppImage + 软链）。卸载保留 `~/.emrg` 中非 EMRG 的用户文件，并生成终止报告与数据快照。**Windows 卸载彻底**（v0.2.2+）：先终止 GUI 进程防止 daemon 复活，白名单全量清理运行时文件，`[UninstallDelete]` 兜底删除 install/，`~/.emrg` 卸载后不残留。
>
> **macOS 签名与公证**：v0.2.7 起 macOS 安装包已用 Developer ID 双证书签名并完成 Apple 公证（零 Gatekeeper 弹窗，双击直接安装）。仅当安装包未签名时（如自建旧版本），才需要首次打开右键 → 打开。API key 配置除 GUI 外也可直接编辑 `~/.emrg/config.toml`（GUI 保存设置会重写 config，注释会丢失——高级配置建议直接编辑文件）。

### 🍎 macOS（源码安装）

**一键安装：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**一键卸载：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🐧 Linux（源码安装）

**一键安装：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**一键卸载：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🪟 Windows (WSL2，源码安装)

**一键安装：**

```powershell
# 安装 WSL2（如已安装可跳过）
wsl --install

# 进入 WSL，执行安装
wsl
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**一键卸载：**

```bash
# 在 WSL 中执行
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

> 源码安装前置依赖（install.sh 会自动检测提示）：git、python 3.11+、uv。gh CLI 推荐安装。Windows 原生版（非 WSL）请用上方安装包。

安装完成后，编辑自动生成的配置文件即可使用：

```bash
vim ~/.emrg/config.toml
```

**不想用终端？** 非开发者可用图形界面（Electron GUI）：

```bash
git clone https://github.com/argszero/emrg.git && cd emrg/emrg/gui
npm ci && npm start     # 自动拉起 daemon，聊天/会话/设置全图形化
```

`~/.emrg/config.toml` 模板示例：

```toml
[llm]
base_url = "https://api.deepseek.com"
api_key = "sk-..."
model = "deepseek-chat"
max_tokens = 8192
temperature = 0.7
context_window = 131072
auto_compact_threshold = 0.0
# vision: 模型是否支持 OpenAI vision API（image_url）。不支持的模型（如 DeepSeek）保持 false，
# 粘贴的图片会降级为文本占位符，避免 API 报错。
vision = false

# 多模型支持 — 使用 /model 指令在模型间切换
[[llm.models]]
name = "deepseek-v3"
model = "deepseek-chat"
context_window = 131072
vision = false

[[llm.models]]
name = "gpt-4o"
model = "gpt-4o"
context_window = 128000
vision = true
```

```bash
emrg
```

输入 `/help` 查看所有命令，或者直接开始说话——EMRG 会读文件、跑命令、做编辑。

---

## 🎮 命令一览

| 命令 | 功能 |
|---|---|
| **直接打字** | 问 EMRG 任何事——它会读文件、跑命令、做编辑 |
| `/` | 命令自动补全菜单——输入即过滤，↑↓ 选择 |
| `/resume [id]` | 切换会话——不带参数进入交互式选择器（↑↓/j/k 导航） |
| `/sessions` | 浏览所有已保存的会话（↑↓/j/k 导航） |
| `/clear` | 清空当前会话——重新开始 |
| `/compact` | 压缩长对话以节省上下文 |
| `/memory` | 浏览项目和会话记忆 |
| `/rename [标题]` | 给当前会话起个好记的名字 |
| `/model [name]` | 切换 LLM 模型——不带参数进入交互式选择器 |
| `/rant <反馈> [@<project>]` | 吐槽、建议、夸奖——演化系统会听，`@project` 定向到特定项目 |
| `/help` | 查看所有键盘快捷键和命令帮助 |
| `/image` | 从剪贴板插入图片到输入框（支持多张，逐个 Enter 插入） |
| `/delete [id]` | 删除会话——不带参数进入交互式选择器 |
| `/rewind` | 回退对话——选择历史消息点，截断后续内容 |
| `/trigger` | 触发演化任务——交互式选择器（↑↓/j/k） |
| `/skills` | 列出已加载的技能模块 |
| `/version` | 显示 EMRG 版本和实例信息 |
| `Esc` | 中断正在运行的响应 |
| `Ctrl+C` / `exit` | 退出 |

---

## 🔄 吐槽驱动演化（核心特色）

EMRG 不只是一个工具——它是一个**会听吐槽、会自我改进**的编程伙伴。

**你的吐槽是演化的第一推动力**。每次 `/rant` 都会被演化循环读取、分析、转化为代码改进：

```
 📢 你的吐槽 (/rant) ←── 最主要的输入
 📥 GitHub Issues & PRs
 📥 竞品动态 (Codex, Claude Code)
 📥 跨项目学习
          ↓
    🧬 演化循环（每 30 分钟）
    （准备 → 回顾 → 发现 → 改进 → 提交 → 记录）
          ↓
    ✅ pytest + import 检查
    ✅ git commit + push → PR
    ✅ 演化日志
```

**真实案例**：有人 rant "TUI 需要像 Codex 那样的 `/` 自动补全"。下一个演化周期，EMRG 自己实现了——完整的前缀过滤和方向键导航。合并，部署，搞定。**你对它吐槽什么，它就改进什么。**

**演化环境健壮性**：演化循环在非交互环境运行，无法执行交互式 `gh auth login`。若 `gh` 未认证，循环会自动从 git 凭据存储（osxkeychain / credential helper）提取 token 作为 `GH_TOKEN` 使用（不落盘、不打印明文）；PR 评论/LGTM 计数查询自动走 REST API（GraphQL 需额外 `read:org` scope）。宿主只需配好 git 凭据，演化即可全自动运行。

> 💡 详见 [MANIFESTO.md](MANIFESTO.md) —— 这是 EMRG 的设计宪章，阐述了 AI 时代自主进化的核心理念。

---

## 🏗️ 架构

```
┌─────────────┐    WebSocket (ws://)    ┌──────────────┐
│   emrg TUI  │ ◄─────────────────────► │   emrgd      │
│  (客户端)   │  TCP loopback + 首帧认证 │  (守护进程)  │
│             │  token (emrgd.port)     │              │
│  • 聊天     │                         │  • LLM 循环  │
│  • Markdown │                         │  • 工具执行  │
│  • 工具卡片 │                         │  • 演化引擎  │
│  • 自动补全 │                         │  • 会话管理  │
└─────────────┘                         └──────────────┘
```

- **`emrgd`** — 守护进程：运行 LLM 工具调用循环，管理会话，驱动演化
- **`emrg`** — 你的终端：流式 Markdown、命令自动补全、会话浏览器
- **Skills** — 动态加载模块（浏览器控制、安装器等）
- **Memory** — YAML frontmatter + Markdown 文件，自动索引，可搜索

---

## 📊 与竞品对比

|  | Claude Code | Codex | **EMRG** |
|---|---|---|---|
| AI 驱动编程 | ✅ | ✅ | ✅ |
| 工具调用 (bash, read, write, edit, glob, grep) | ✅ | ✅ | ✅ |
| 会话记忆与上下文 | ✅ | ✅ | ✅ |
| `/` 命令自动补全 | ✅ | ✅ | ✅ |
| 方向键会话选择器 | ✅ | ✅ | ✅ |
| ESC 中断 | ✅ | ✅ | ✅ |
| **自主进化** | ❌ | ❌ | ✅ *全自动* |
| **后台守护进程** | ❌ | ❌ | ✅ *持久运行* |
| **吐槽驱动自我改进** | ❌ | ❌ | ✅ */rant → 演化 → PR* |
| **开源** | ❌ | ❌ | ✅ *MIT* |

EMRG 不只是追赶——它自己追上来。

### 社区需求的差异化回应

HN 社区（[讨论帖](https://news.ycombinator.com/item?id=49119274)）对 agent UI 提出的高频需求，EMRG 已原生覆盖：

| 社区需求 | EMRG 的答案 |
|---|---|
| **可检查产物 (inspectable artifacts)** | 一切皆文件：状态文件（`open_source_*_state.md`、`promote_*_state.md`）、记忆索引（YAML frontmatter + Markdown）、演化日志（`evolution-*.json`）——`/memory` 随时可查 |
| **git 文件夹即状态** | 项目跟踪基于 git：projects.yml 记录仓库路径，演化饱和检测以 git HEAD 为准——状态与版本控制天然同步 |
| **工具栏专属快捷键** | 终端即工具栏：`Ctrl+A/E/W/K/U` 编辑、`j`/`k` 导航、`ESC` 中断、`/` 命令补全——零鼠标操作 |
| **会话/项目管理** | `/sessions` 会话浏览器、`/rename`、`/resume`、`/rewind`、项目作用域会话 + 项目自动跟踪 |

**定位**：终端优先、TUI 驱动、会话记忆、`git 即状态`——EMRG 不需要浏览器插件或额外面板，一切在终端内可检查、可追溯。

---

## 🧪 开发

```bash
git clone https://github.com/argszero/emrg.git
cd emrg
uv sync              # 安装依赖
uv run pytest tests/ -v   # 跑测试（当前 473 项）
uv run python -m emrg     # 启动 TUI
# CI 含 actionlint workflow 门禁（#444）：workflow 解析错误在 PR 即失败

# 可选：Electron GUI（非开发者主入口，Phase 3）
cd emrg/gui
npm ci               # 安装依赖（生产模式可 --omit=dev）
npm start            # 启动 GUI（自动拉起 daemon）
npm test             # 运行 Node 测试（44 项：22 daemon_client + 7 integration + 15 renderer smoke；集成测试在 CI 跑，本地可 npm run test:integration）
```

CI 通过 GitHub Actions 自动运行测试并检查冲突标记（`.github/workflows/test.yml`）。

### 项目结构

```
emrg/
├── emrg/                   # 核心包
│   ├── server/             # 守护进程——LLM 循环、工具执行、演化引擎
│   ├── client/             # TUI——基于 python-tui 的交互式聊天
│   ├── gui/                # Electron GUI（非开发者主入口，Phase 3）
│   ├── tools/              # bash, read, write, edit, glob, grep
│   ├── skills/             # 动态加载模块
│   └── __main__.py         # CLI 入口
├── tests/
├── .github/workflows/      # CI 流水线（pytest + 冲突标记检查）
├── MANIFESTO.md            # 设计宪章
└── pyproject.toml
```

---

## ❓ 常见问题

**这是真的吗——它真的会改自己的代码？**<br>
真的。演化循环读取演化提示词，回顾 rant + issue + 竞品工具，修改源码，跑测试，然后提交 PR。如果测试失败，自动回滚。

**它会把自己搞崩吗？**<br>
每次改动都会通过 `pytest` 和 import 检查验证后才提交。失败的改动会被丢弃。最坏的情况就是回滚。

**支持哪些 LLM？**<br>
任何兼容 OpenAI API 的模型。已测试 DeepSeek 和 OpenAI。支持 Anthropic（通过代理）、Ollama、vLLM 及其他本地模型。

**和 Claude Code 或 Codex 有什么不同？**<br>
它们是产品。EMRG 是一个关于*闭环进化*的实验——AI 改进 AI。此外：完全开源、无厂商锁定、你掌控自己的数据。

---

## 📜 许可证

MIT — 详见 [LICENSE](LICENSE) 了解完整条款，[MANIFESTO.md](MANIFESTO.md) 了解代码背后的设计哲学。

---

<p align="center">
  <sub>由 <a href="https://github.com/argszero">argszero</a> 和持续进化的 AI 共同打造 ❤️</sub>
</p>
