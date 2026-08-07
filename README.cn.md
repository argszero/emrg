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
  <a href="README.md">🇬🇧 English</a> | <b>🇨🇳 中文</b>
</p>

---

**如果每次使用，你的编程助手都在变得更好，会怎样？**

EMRG 是一个关于*自主进化*的实验。它能帮你写代码——读文件、跑命令、做编辑——但核心差异在于：**你的每一条吐槽 (`/rant`) 都会驱动它自我改进**。你说它哪里不好，下一个演化周期它就自己写代码改。配合 GitHub 社区动态和竞品更新，EMRG 在后台持续进化，越用越顺手。完全开源，完全透明。

> *"EMRG 是一个自我进化的 AI 智能体架构实验。"* — [MANIFESTO](MANIFESTO.md)

---

## ✨ 为什么你会爱上它

**一句话定位**：EMRG 是唯一一个**会因你的反馈而自我改进**的编程智能体——吐槽哪里不好，下一个演化周期它就自己写代码修好并上线。聊天、工具、记忆是标配；自我进化闭环才是它独一无二的地方。

| 特性 | 说明 |
|---|---|
| 🔄 **吐槽驱动进化（核心）** | 你的 `/rant` 直接驱动后台演化循环——吐槽 → 分析 → 写代码 → 提 PR → 自动变强。打包安装版演化工作区自愈：按需 clone 仓库 + 自动补齐 projects/tasks 配置 |
| 🖥️ **Electron GUI（主入口）** | 安装即用：首次启动引导配置 API Key；全部 15 个 `/` 指令 GUI 可用（`/rant` 进化对话框、`/memory` 记忆浏览器…）；WorkBuddy 启发的结果面板、Ask/Auto 模式、自进化可见化（成长卡 + toast）。GUI 配好即 TUI 可用 |
| 🧠 **读写改跑，样样精通** | 完整的工具调用能力——bash、文件读写、diff 编辑、glob、grep |
| 📝 **永不忘事** | 项目记忆 + 会话记忆 + 每日日志——上下文持续保留，不怕断线 |
| ⚡ **全功能 TUI + 守护进程** | 流式 Markdown、`/` 自动补全、会话选择器、ESC 中断、Vim 友好按键、并行工具调用——跑在持久化的 `emrgd` 守护进程上，随时重连 |
| 🌍 **100% 开源** | MIT 协议——没有围墙，没有厂商锁定。面向国际化：默认英文，提供中文版 |

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

全自动无人值守运行：`gh` 未认证时自动从 git 凭据提取 token、PR 投票走 REST API、打包安装版工作区自愈（按需 clone + 自动补齐配置）。详见 [MANIFESTO.md](MANIFESTO.md) —— EMRG 的设计宪章。

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

> **Windows SmartScreen 提示**：Windows 安装包未做 Authenticode 签名（发布者显示"未知"），首次下载/运行时 SmartScreen 可能提示"通常不会下载此文件"或"Windows 已保护你的电脑"。这是未签名软件的常见安全提醒，不代表文件有问题（EMRG 完全开源，源码可审计）。放行方法：
> (1) 浏览器下载提示 → 点**保留**（或三个点 → 保留）
> (2) 双击 exe 若提示"Windows 已保护你的电脑" → 点**更多信息** → 点**仍要运行**
> (3) 或右键 exe → 属性 → 勾选**解除锁定**（若有）→ 确定 → 双击运行

安装包内置完整运行时（standalone Python 3.13 + 依赖 + git + gh + GUI），**干净机器（无 python/uv/git/gh/node）零前置依赖**，100% 离线安装。安装后：

**三步开始使用：**
1. 启动台 / 开始菜单点击 **EMRG**（GUI）
2. 首次启动引导配置 **API Key / 模型**
3. 开始对话——**TUI 同步可用**（新开终端运行 `emrg`）

> **卸载**：macOS 运行"卸载 EMRG.app"；Windows 控制面板卸载；Linux 运行 `~/.emrg/install/bin/emrg-uninstall`（或删 AppImage + 软链）。卸载保留 `~/.emrg` 中非 EMRG 的用户文件，并生成终止报告与数据快照。
>
> **macOS 签名与公证**：v0.2.7 起 macOS 安装包已用 Developer ID 双证书签名并完成 Apple 公证（零 Gatekeeper 弹窗，双击直接安装）。仅当安装包未签名时（如自建旧版本），才需要首次打开右键 → 打开。

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

### 🖥️ 首次配置（GUI 第一）

安装完成后，**打开 GUI 完成首次配置**：

1. **macOS**：启动台 → 点击 **EMRG**；**Windows**：开始菜单 → **EMRG**
2. 首次启动引导会带你配置 **API Key / 接口地址 / 模型**（也随时可在设置 ⚙ 中修改）
3. 保存后即可开始对话——**配置写入 `~/.emrg/config.toml`，GUI 与 TUI 共享**

> 💡 **GUI 配好，TUI 直接用**：安装包内置完整 TUI。GUI 保存的配置（API Key/模型/工作目录）写入 `~/.emrg/config.toml`，终端新开窗口运行 `emrg` 即进入 TUI，无需重复配置。v0.2.8 起 GUI 支持**全部 15 个 / 指令**——输入框输入 `/` 弹出补全菜单（与 TUI 一致）。

### ⌨️ 使用 TUI

```bash
emrg
```

输入 `/help` 查看所有命令，或者直接开始说话——EMRG 会读文件、跑命令、做编辑。

### 🔧 高级配置（可选）

> GUI 保存设置会重写 config 并丢失注释——高级用户可直接编辑 `~/.emrg/config.toml`（首次配置无需手动编辑，GUI 引导即可）。

`~/.emrg/config.toml` 模板示例（GUI 保存后自动生成等价内容）：

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

---

## 🎮 命令一览

> v0.2.8 起**全部命令在 GUI 与 TUI 均可使用**。GUI 中输入框敲 `/` 弹出补全菜单；TUI 中 `/help` 列出全部。

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

---

## 🧪 开发

```bash
git clone https://github.com/argszero/emrg.git
cd emrg
uv sync              # 安装依赖
uv run pytest tests/ -v   # 跑测试（当前 508 项）
uv run python -m emrg     # 启动 TUI
# CI 含 actionlint workflow 门禁（#444）：workflow 解析错误在 PR 即失败

# 可选：Electron GUI（非开发者主入口，Phase 3）
cd emrg/gui
npm ci               # 安装依赖（生产模式可 --omit=dev）
npm start            # 启动 GUI（自动拉起 daemon）
npm test             # 运行 Node 测试（91 项：22 daemon_client + 22 app-commands + 22 renderer smoke + 15 i18n + 7 integration + 3 commands；集成测试在 CI 跑，本地可 npm run test:integration）
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

**为什么 Windows 安装包会提示"未知发布者"？**<br>
Windows 安装包未做 Authenticode 签名（该证书需付费申请，暂不采购），因此 SmartScreen 会显示"发布者：未知"并可能阻止运行。这是微软对新发布/未签名软件的通用安全提醒，**不代表文件有问题**——EMRG 完全开源（MIT），源码可审计。放行：浏览器提示点"保留"；运行提示点"更多信息 → 仍要运行"；或右键 exe → 属性 → 勾选"解除锁定"。macOS 安装包已签名+公证（v0.2.7+），无此问题。

---

## 📜 许可证

MIT — 详见 [LICENSE](LICENSE) 了解完整条款，[MANIFESTO.md](MANIFESTO.md) 了解代码背后的设计哲学。

---

<p align="center">
  <sub>由 <a href="https://github.com/argszero">argszero</a> 和持续进化的 AI 共同打造 ❤️</sub>
</p>
