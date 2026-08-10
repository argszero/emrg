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

EMRG 是一个关于*自主进化*的实验：一个能帮你写代码的 AI 智能体——读文件、跑命令、做编辑——而**你的每一条吐槽 (`/rant`) 都会驱动它自我改进**。你说它哪里不好，下一个演化周期它就自己写代码修好并上线。完全开源，完全透明。

> *"EMRG 是一个自我进化的 AI 智能体架构实验。"* — [MANIFESTO](MANIFESTO.md)

---

## ✨ 为什么你会爱上它

**一句话定位**：EMRG 是唯一一个**会因你的反馈而自我改进**的编程智能体——吐槽哪里不好，下一个演化周期它就自己写代码修好并上线。

| 特性 | 差异点 |
|---|---|
| 🔄 **越用越好（核心）** | 你的 `/rant`、GitHub Issues、竞品动态都会变成真实改进——分析、编码、测试、提 PR、合并，全自动无人值守 |
| 🖥️ **Electron GUI（主入口）** | 安装即用：首次启动引导配置 API Key；全部 `/` 指令可用；结果面板、Ask/Auto 模式、自进化可见化。GUI 配好即 TUI 可用 |
| ⚡ **全功能 TUI + 守护进程** | 流式 Markdown、`/` 自动补全、会话管理、快捷键——跑在常驻的 `emrgd` 守护进程上，随时重连 |
| 🌍 **100% 开源** | MIT 协议——没有围墙，没有厂商锁定。默认英文，提供中文版 |

---

## 🔄 吐槽驱动演化（核心特色）

EMRG 不只是一个工具——它是一个**会听吐槽、会自我改进**的编程伙伴：

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

**真实案例**：有人吐槽 "TUI 需要像 Codex 那样的 `/` 自动补全"。下一个演化周期，EMRG 自己实现了——完整的前缀过滤和方向键导航，合并、部署、搞定。**你对它吐槽什么，它就改进什么。**

---

## 🚀 快速开始

### 📦 下载安装包（推荐）

到 [GitHub Releases](https://github.com/argszero/emrg/releases) 下载对应平台的安装文件，双击安装即可——安装包内置完整运行时（Python 3.13 + git + gh + GUI），**零前置依赖，离线安装**：

| 平台 | 安装文件 |
|------|---------|
| macOS | `EMRG-<ver>-macos-arm64.pkg` / `-x64.pkg`（用户级安装，无需管理员密码） |
| Windows | `EMRG-<ver>-windows-x64.exe`（免 UAC，PATH 自动注册） |
| Linux | `EMRG-<ver>-linux-x86_64.AppImage` / `-aarch64.AppImage` |

> **Windows SmartScreen 提示**：安装包未做 Authenticode 签名——如 SmartScreen 提示，点**保留** / **更多信息 → 仍要运行**。EMRG 完全开源，源码可审计。

**首次使用**：打开 **EMRG** → 引导配置 **API Key / 模型** → 开始对话。TUI 同步可用：任意终端运行 `emrg` 即可（配置共享）。

> 想从源码安装，或需要高级配置 / 架构 / 贡献者文档？→ [DEVELOPMENT.md](DEVELOPMENT.md)

---

## 🎮 命令一览

> 全部命令在 GUI 与 TUI 均可使用（GUI 输入框敲 `/` 弹出补全菜单；TUI `/help` 列出全部）。

| 命令 | 功能 |
|---|---|
| **直接打字** | 问 EMRG 任何事——它会读文件、跑命令、做编辑 |
| `/` | 命令自动补全菜单——输入即过滤，↑↓ 选择 |
| `/resume [id]` | 切换会话——不带参数进入交互式选择器（↑↓/j/k） |
| `/sessions` | 浏览所有已保存的会话（↑↓/j/k） |
| `/clear` | 清空当前会话——重新开始 |
| `/compact` | 压缩长对话以节省上下文 |
| `/memory` | 浏览项目和会话记忆 |
| `/rename [标题]` | 给当前会话起个好记的名字 |
| `/model [name]` | 切换 LLM 模型——不带参数进入交互式选择器 |
| `/rant <反馈> [@<project>]` | 吐槽、建议、夸奖——演化系统会听 |
| `/help` | 查看所有键盘快捷键和命令帮助 |
| `/image` | 从剪贴板插入图片到输入框 |
| `/delete [id]` | 删除会话——不带参数进入交互式选择器 |
| `/rewind` | 回退对话到历史某个节点 |
| `/trigger` | 触发演化任务——交互式选择器 |
| `/skills` | 列出已加载技能；`/skills available\|install\|update` |
| `/version` | 显示 EMRG 版本和实例信息 |
| `Esc` | 中断正在运行的响应 |
| `Ctrl+C` / `exit` | 退出 |

---

## 📊 与竞品对比

|  | Claude Code | Codex | **EMRG** |
|---|---|---|---|
| AI 驱动编程 | ✅ | ✅ | ✅ |
| 工具调用 (bash, read, write, edit, glob, grep) | ✅ | ✅ | ✅ |
| 会话记忆与上下文 | ✅ | ✅ | ✅ |
| `/` 命令自动补全 | ✅ | ✅ | ✅ |
| ESC 中断 | ✅ | ✅ | ✅ |
| **自主进化** | ❌ | ❌ | ✅ *全自动* |
| **后台守护进程** | ❌ | ❌ | ✅ *持久运行* |
| **吐槽驱动自我改进** | ❌ | ❌ | ✅ */rant → PR* |
| **开源** | ❌ | ❌ | ✅ *MIT* |

EMRG 不只是追赶——它自己追上来。

---

## ❓ 常见问题

**这是真的吗——它真的会改自己的代码？**<br>
真的。演化循环读取演化提示词，回顾 rant + issue + 竞品工具，修改源码，跑测试，然后提交 PR。如果测试失败，自动回滚。

**它会把自己搞崩吗？**<br>
每次改动都会通过 `pytest` 和 import 检查验证后才提交。失败的改动会被丢弃；最坏的情况就是回滚。

**和 Claude Code 或 Codex 有什么不同？**<br>
它们是产品。EMRG 是一个关于*闭环进化*的实验——AI 改进 AI。完全开源，无厂商锁定。

---

## 🧪 开发

贡献指南、源码安装、架构、详细 FAQ → [DEVELOPMENT.md](DEVELOPMENT.md)。

快速检查：`uv run pytest tests/ -v`（当前 681 项）· `cd emrg/gui && npm test`（178 项：43 daemon_client + 19 conn-manager + 22 app-commands + 59 renderer smoke + 15 i18n + 7 integration + 3 commands + 3 build-config + 7 gui-state）

---

## 📜 许可证

MIT — 详见 [LICENSE](LICENSE) 了解完整条款，[MANIFESTO.md](MANIFESTO.md) 了解代码背后的设计哲学。

---

<p align="center">
  <sub>由 <a href="https://github.com/argszero">argszero</a> 和持续进化的 AI 共同打造 ❤️</sub>
</p>
