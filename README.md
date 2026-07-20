# 🧱 EMRG

<p align="center">
  <strong>一个会写代码的 AI 编程助手——而且它还会<em>重写自己</em>。</strong>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10+-blue.svg">
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
| ⚡ **并行工具调用** | 独立的工具调用并发执行，速度飞快 |
| 🔌 **微内核守护进程** | `emrgd` 持久运行——随时重连，状态不丢 |
| 🎮 **Vim 友好** | `j`/`k` 导航、`Ctrl+W`/`Ctrl+K` 编辑、`Tab` 展开工具卡片 |
| 🌍 **100% 开源** | MIT 协议——没有围墙，没有厂商锁定 |

---

## 🚀 快速开始

### 🍎 macOS

**一键安装：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**一键卸载：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🐧 Linux

**一键安装：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

**一键卸载：**

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash -s -- purge
```

### 🪟 Windows (WSL2)

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

> 前置依赖（install.sh 会自动检测提示）：git、python 3.10+、uv。gh CLI 推荐安装。

安装完成后，编辑自动生成的配置文件即可使用：

```bash
# 安装脚本已自动生成 ~/.emrg/config.toml 模板，只需修改 api_key 和 model：
vim ~/.emrg/config.toml

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
| `/rant <反馈> [@<project>]` | 吐槽、建议、夸奖——演化系统会听，`@project` 定向到特定项目 |
| `/help` | 查看所有键盘快捷键和命令帮助 |
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

> 💡 详见 [MANIFESTO.md](MANIFESTO.md) —— 这是 EMRG 的设计宪章，阐述了 AI 时代自主进化的核心理念。

---

## 🏗️ 架构

```
┌─────────────┐     Unix Socket IPC     ┌──────────────┐
│   emrg TUI  │ ◄──────────────────────► │   emrgd      │
│  (客户端)   │   JSON 换行分隔           │  (守护进程)  │
│             │                          │              │
│  • 聊天     │                          │  • LLM 循环  │
│  • Markdown │                          │  • 工具执行  │
│  • 工具卡片 │                          │  • 演化引擎  │
│  • 自动补全 │                          │  • 会话管理  │
└─────────────┘                          └──────────────┘
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
uv run pytest tests/ -v   # 跑测试（当前 159 项）
uv run python -m emrg     # 启动
```

CI 通过 GitHub Actions 自动运行测试并检查冲突标记（`.github/workflows/test.yml`）。

### 项目结构

```
emrg/
├── emrg/                   # 核心包
│   ├── server/             # 守护进程——LLM 循环、工具执行、演化引擎
│   ├── client/             # TUI——基于 python-tui 的交互式聊天
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
