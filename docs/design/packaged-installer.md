# 独立安装包设计：从"脚本安装"到"下载 → 点击安装"

## 问题分析

### 当前状态

EMRG 目前的安装方式是脚本引导：

```bash
curl -sSL https://raw.githubusercontent.com/argszero/emrg/master/install.sh | bash
```

`install.sh` 会依次检查并安装 4 个外部依赖：

| 依赖 | 用途 | 安装方式 |
|------|------|----------|
| git | 克隆源码、演化提交 | brew / apt / 自带 |
| python3 ≥ 3.11 | 运行解释器 | brew / apt |
| uv | 依赖管理 + 工具安装 | curl 脚本 |
| gh (推荐) | 演化系统的 GitHub 操作 | brew / 二进制下载 |

安装后源码存放在 `~/.emrg/source/`，通过 `uv tool install -e .` 以 editable 模式挂载。

### 问题

1. **外部依赖多**：一台干净的机器需要先有 git + python3 + uv，install.sh 虽然会尝试自动装，但 brew/apt/sudo 每一步都可能失败或需要交互。
2. **curl | bash 信任门槛高**：对非开发者用户不友好，"把远程脚本直接灌进 shell" 是安全反模式。
3. **源码部署**：装的是源码树 + editable 挂载，用户机器上多了一个需要维护的 git 仓库；`emrg update` 依赖 git pull。
4. **平台体验不一致**：macOS/Linux/Windows 各走各的路，没有统一的"安装应用"体验。

### 目标

- **用户视角**：像安装普通应用一样 —— 下载一个安装文件 → 双击/点击 → 完成，无需预装 python/uv/git。
- **安装即完整**：Python 解释器 + 全部第三方库 + git + gh 全部捆绑进安装包，装完就是功能完整的 EMRG，不依赖任何外部环境。
- **统一安装方式**：不区分开发者/非开发者，每平台只有一种官方安装方式、一个安装文件。
- **卸载彻底**：一个卸载入口，清空全部安装痕迹（二进制、符号链接、PATH、数据目录），不留残留。
- **双界面前瞻**：同一安装包同时提供 TUI 与 GUI 两种客户端（§7），共享同一个守护进程，安装一次两种界面都可用。

---

## 方案选型

| 方案 | 说明 | 捆绑 Python | 体积 | 成熟度 | 结论 |
|------|------|:---:|:---:|:---:|------|
| **A. PyInstaller + 平台安装器** | Python 应用打包为原生可执行文件，再用 pkg/exe/AppImage 包装 | ✅ | ~60-90MB | 极高（30 年历史） | ✅ **推荐** |
| B. Nuitka | Python→C 编译，启动快、更难反编译 | ✅ | ~40-60MB | 高但配置复杂 | 备选，Phase 2 评估 |
| C. uv standalone python + 源码 | 下载官方 standalone CPython + 依赖目录 | ✅ | ~50MB | 中 | 无原生安装体验，放弃 |
| D. Rust 重写 | 彻底去除 Python 依赖 | 无需 | ~10MB | 项目早期从 Rust 移植而来 | 远期路线，不在本文范围 |

**选 A 的理由**：
- PyInstaller 是打包 Python CLI + 守护进程的标准方案，对 asyncio/httpx/rich/yaml/jinja2 生态支持成熟。
- 产物是原生可执行文件，天然支持平台安装器包装。
- 源码与打包产物解耦：打包只是构建期的附加步骤，不污染开发流程。

---

## 目标安装体验（统一）

**每平台一种安装方式、一个安装文件。开发者与非开发者完全一致。**

| 平台 | 安装文件 | 体验 | 安装位置 |
|------|----------|------|----------|
| macOS | `EMRG-<ver>.pkg` | 双击 → 安装向导 → 完成；GUI 入口出现在启动台/应用程序 | `~/.emrg/install/`（用户级）+ PATH |
| Windows | `EMRG-Setup-<ver>.exe` | 双击 → 安装向导 → 完成；开始菜单出现 GUI 快捷方式 | `%LOCALAPPDATA%\EMRG\` + PATH |
| Linux | `EMRG-<ver>-linux-<arch>.AppImage` | 下载 → chmod +x → 双击运行（GUI）；桌面环境图标 | 单文件，`emrg` 符号链接到 `~/.local/bin` |

> **设计决策：用户级安装（免 sudo，跨平台统一）**。
> - 系统级路径（/usr/local、Program Files）需要管理员权限，且 `emrg update` 自动更新会遇到权限问题；
> - macOS/Windows 的安装向导做的事完全一致：安置文件到用户目录 + 写 PATH；
> - Linux 用 **AppImage**（单文件、无需安装、chmod +x 即运行）——这是 Linux 生态里最接近"下载 → 双击"的格式，**不需要任何安装脚本**。首次运行后 `emrg` 自动在 `~/.local/bin` 建符号链接；用户也可以直接把 AppImage 移到任意目录运行；
> - 服务器/无 GUI 环境可用 `EMRG-<ver>-linux-<arch>.tar.gz`（与 AppImage 同内容的解压版）作为补充，但官方统一体验是 AppImage。

### 安装产物目录结构（平台内统一）

> AppImage / tarball / pkg 内部装载的都是同一套目录（AppImage 是 squashfs 封装，pkg 是目录 + 脚本，tarball 是压缩目录）。

```
<prefix>/
├── bin/
│   ├── emrg                # TUI 客户端 + CLI（PyInstaller 引导器）
│   ├── emrg-gui            # GUI 客户端（未来；见 §7 多客户端架构）
│   ├── emrgd               # 守护进程（独立入口，生命本体）
│   ├── git                 # 捆绑 git（Windows: portable Git；macOS/Linux: 便携构建）
│   ├── gh                  # 捆绑 gh CLI（Go 单文件二进制）
│   └── _internal/          # PyInstaller 运行时（解释器 + 依赖 + 模板数据）
├── LICENSE
└── version.txt
```

数据目录不变：`~/.emrg/`（config.toml、sessions、memory、logs、projects.yml、tasks.yml、versions/、saturation/）。

> 注意 `install/`（安装产物）与 `~/.emrg/`（数据）同在一个父目录下，但语义严格分离：`install/` 只读可替换，数据目录由运行时读写。Linux 的 AppImage 与 tarball 内部结构相同，AppImage 仅多了自挂载封装。

---

## 架构设计

### 1. 双入口二进制

当前 daemon 由 client 通过 `subprocess` 启动：

```python
# emrg/__main__.py:_start_daemon_background()  现状
proc = subprocess.Popen([sys.executable, "-m", "emrg.server"], ...)
```

PyInstaller 下 `sys.executable` 指向打包引导器，`-m emrg.server` 不再可用。改造为：

```python
# 改造后
def _start_daemon_background() -> subprocess.Popen:
    cleanup_server()
    if getattr(sys, "frozen", False):
        # 打包模式：启动同目录的 emrgd 二进制
        emrgd = Path(sys.executable).resolve().parent / "emrgd"
        proc = subprocess.Popen([str(emrgd)], ...)
    else:
        # 源码模式：保持现状
        proc = subprocess.Popen([sys.executable, "-m", "emrg.server"], ...)
    return proc
```

两个 PyInstaller 入口：
- `emrg` ← `emrg/__main__.py:main()`（CLI 全家桶：client / server stop / rant / update）
- `emrgd` ← `emrg/server/__main__.py`（守护进程，独立进程，长期运行）

`emrg server`（前台运行）在打包模式下亦可用 `emrgd` 等价实现。

### 2. 数据文件打包清单

PyInstaller 默认只收集 Python 模块，以下资源文件必须显式声明为 `datas`：

```
emrg/server/prompts/system.j2        # 系统提示词模板
emrg/server/evolution_prompt.md      # 演化提示词
emrg/server/promote_prompt.md        # promote 任务提示词
emrg/server/open_source_prompt.md    # 开源任务提示词
emrg/server/paper_prompt.md          # paper 任务提示词
```

注意 `daemon.py` 与 `scheduler.py` 中模板路径的解析方式：

```python
# 现状：基于 __file__ 的文件系统路径（打包后 __file__ 在 _MEIPASS 内部，可行）
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "prompts"),
    ...
)
```

`__file__` 在 PyInstaller 下指向 `_MEIPASS` 内路径，FileSystemLoader 可正常工作，但必须：
- 用 `--collect-data emrg` 或 spec 文件 `datas` 显式收集；
- 为只读资源（运行时不会被修改的文件）验证只读路径——演化引擎当前会写 `promote_prompt.md` 状态文件，需确认这些状态文件写入的是 `~/.emrg/` 而非安装目录（见下文 §3）。

### 3. 运行时可变文件审计

打包后安装目录是只读的（用户可能无写权限）。审计所有运行时写入路径：

| 写入点 | 当前目标 | 打包后 | 状态 |
|--------|----------|--------|------|
| `~/.emrg/config.toml` | 用户目录 | 不变 | ✅ 安全 |
| `~/.emrg/projects.yml` | 用户目录 | 不变 | ✅ 安全 |
| `~/.emrg/rants.jsonl` | 用户目录 | 不变 | ✅ 安全 |
| `~/.emrg/tasks.yml` | 用户目录 | 不变 | ✅ 安全 |
| `~/.emrg/logs/`、`saturation/`、`skills/` | 用户目录 | 不变 | ✅ 安全 |
| 会话/记忆（`<cwd>/.emrg/`） | 工作目录 | 不变 | ✅ 安全 |
| `emrg/server/*.md` 状态文件（promote/open-source 任务的 reflection/state 文件） | 源码树内 `prompts/` 或 session 目录 | **需迁移到 session 目录** | ⚠️ 审计项 |

> 已有进展：最近的 commit（`e78d355`）已把 promote 状态文件路径改为 session 目录。打包前需完成全量审计，确保**安装目录零写入**。

### 4. git / gh 捆绑（安装即完整）

演化系统的核心能力（commit、PR、merge、上游监控）依赖 git 与 gh。既然"安装要完整"，二者**随包捆绑**，不依赖用户预装：

| 平台 | git 来源 | gh 来源 |
|------|----------|---------|
| Windows | 捆绑 Git for Windows（portable 版，官方单文件自解压） | gh 官方 release 单文件二进制 |
| macOS | 捆绑便携 git 构建（编译时随 CI 下载） | gh 官方 release 单文件二进制 |
| Linux | 捆绑静态/便携 git 构建 | gh 官方 release 单文件二进制 |

**运行时解析顺序**（`git_utils.py` 与演化 prompt 统一走同一解析器）：

```
定位 git/gh:
  1. 打包模式：优先 ~/.emrg/install/bin/git（捆绑版，版本一致、行为确定）
  2. 兜底：系统 PATH 中的 git/gh（用户自装且版本满足时）
```

- EMRG 启动时探测一次，把解析结果写入 `~/.emrg/install-info.json`，演化 prompt 通过模板变量注入（`{{ git_path }}`、`{{ gh_path }}`）。
- **AppImage 特例**：Linux AppImage 运行时挂载到临时目录，`bin/git` 通过 `os.environ["APPIMAGE"]` / `/proc/self/mounts` 定位，或首次运行时把 git/gh 复制到 `~/.emrg/install/bin/`（数据目录，可写）。实现细节 Phase 1 验证。
- gh 的认证仍由用户完成（`gh auth login`，OAuth 流程不可自动化）——首次运行演化任务时提示。捆绑 gh 解决的是"有 gh 可用"，认证是用户账号层面的必然步骤。
- **为什么不用 PATH 优先**：系统 git 版本参差（旧版缺 `git pull --rebase` 等行为差异），捆绑版保证演化引擎行为在所有机器上一致。

### 5. 自动更新改造

现状：`emrg update` = `git pull` + `uv tool install --reinstall -e .`。

打包后改为**二进制自更新**（用户级，免 sudo）：

```
emrg update
  → GET https://api.github.com/repos/argszero/emrg/releases/latest   # 查版本 + SHA256
  → 从该 release 附件下载 emrg-<ver>-<os>-<arch>.tar.gz（校验 SHA256）
  → 解压到 ~/.emrg/versions/<ver>/（新版本目录）
  → 原子切换符号链接 ~/.emrg/bin/emrg → 新版
  → 重启守护进程（复用现有 emrg server restart 逻辑）
  → 保留旧版本目录，失败自动回退
```

- 版本目录 + 符号链接设计保证"更新中断"不会破坏现有安装（回滚 = 换回旧链接）。
- `emrgd` 同理更新；若 daemon 正在运行，先优雅停止（现有 shutdown 协议）再替换。
- 更新只替换 `install/` 下的文件，**不触碰数据目录**（sessions/memory/config 等）。

### 6. 卸载设计（全面清理）

对齐 MANIFESTO 第十条【终止权】：生成终止报告、留存经验快照、删除运行时文件、卸载自身。**不删除宿主工作目录下的用户数据**（`<cwd>/.emrg/` 属宿主数据）。

统一卸载入口：

| 平台 | 卸载方式 |
|------|----------|
| macOS | 双击 pkg 卸载器（或 `emrg uninstall`） |
| Windows | 控制面板卸载（Inno Setup 生成 unins000.exe，或 `emrg uninstall`） |
| Linux | 删除 AppImage 文件 + `emrg uninstall`（或 tarball 解压版同理） |

`emrg uninstall` 执行顺序（幂等，任一步失败可重跑）：

```
1. 停止并关闭 daemon（现有 shutdown 协议，等待退出）
2. 生成终止报告 → ~/.emrg/logs/uninstall-report-<ts>.json
   （实例身份、演化次数、会话数、记忆数、终止原因=用户主动）
3. 留存经验墓地快照（不可删除项）：
   → 打包记忆 + 会话 + 演化日志 → ~/.emrg/graveyard/emrg-data-<ts>.tar.gz
   → 快照保留，卸载后用户可自行删除
4. 删除运行时文件：
   → ~/.emrg/install/（二进制、_internal、模板）
   → ~/.emrg/versions/（历史版本目录）
   → ~/.emrg 下其余运行时文件（config.toml、sessions、memory、logs、
     projects.yml、tasks.yml、rants.jsonl、saturation/、emrgd.sock/pid、
     install-info.json）
5. 清理环境痕迹：
   → PATH 中移除 emrg 条目（macOS/Linux 从 shell rc 文件；
     Windows 从注册表用户 PATH）
   → Windows: 移除开始菜单/桌面快捷方式（如创建过）
6. 自校验：确认 emrg/emrgd 命令已不可用、目录已删除，输出清理清单
```

**边界**：
- 宿主工作目录下的 `.emrg/`（会话、记忆的 project 副本）**不删除**——那是用户项目数据，由宿主决定去留，卸载报告会列出其位置。
- 卸载幂等：重复执行不会报错，未找到的项目跳过并说明。
- 与 `emrg update` 的关系：更新只动 `install/`，卸载才清数据——互不干扰。

### 7. 多客户端架构（TUI + GUI 双客户端）

**愿景**：一个 EMRG 实例同时提供 TUI 与 GUI 两种界面。二者是**平等的客户端入口**，共享同一个守护进程（生命本体），不重复承载任何生命逻辑。用户可同时开着 GUI 和 TUI 连接同一 daemon。

```
                 ┌──────────────────────────────────┐
                 │            emrgd（唯一）           │
                 │  生命本体：LLM 循环 / 工具执行 /     │
                 │  演化引擎 / 会话 / 记忆 / 项目跟踪   │
                 └──────────────────────────────────┘
                    ▲             ▲             ▲
        Unix Socket / Named Pipe IPC（同一协议）
                    │             │             │
        ┌───────────┴──┐   ┌──────┴─────┐   ┌───┴────────┐
        │  emrg (TUI)  │   │ emrg-gui   │   │ 未来客户端  │
        │  终端界面      │   │ 图形界面    │   │ (mobile/… )│
        └──────────────┘   └────────────┘   └────────────┘
```

**架构基础已具备（零改造）**：
- daemon 用 `serve_forever()` 为每个连接创建独立 `_handle_client` 协程，每个连接有独立的工具任务（`_tool_task`）——多客户端**并发连接**开箱即用；
- IPC 协议（`protocol.py` + 长度前缀分帧）与传输（`connect.py`，Unix Socket / Named Pipe）完全平台无关，GUI 复用同一协议层；
- 会话、记忆、演化全部由 daemon 管理，客户端只做渲染与交互——GUI 不引入第二份状态。

**GUI 技术栈选型**（已定稿：**Electron**，2026-08-03 用户决策）：

| 方案 | 体积增量 | 平台自包含性 | 现代 UI 能力 | 结论 |
|------|:---:|------|:---:|------|
| **Electron** | ~80-200MB | ✅ 全平台捆绑 Chromium | 最强（HTML/CSS/JS 生态） | **已选**：UI 能力最强，Markdown/diff 现成生态；WebSocket 协议语言无关，Node 原生 `ws` 直连 |
| PySide6 (Qt) | ~80-120MB | ✅ 全平台捆绑 Qt 库 | 强（WebEngine/原生控件） | 备选（被 Electron 取代） |
| pywebview（系统 WebView） | ~10-30MB | macOS ✅ / Windows ✅(WebView2) / Linux ⚠️(需 WebKitGTK) | 强（HTML/CSS/JS） | 备选：体积小，但 Linux 引入系统依赖 |
| Tkinter（stdlib） | ~5MB | ✅ 随 Python 捆绑 | 弱（控件老旧） | 兜底：体积最小，体验一般 |

> **决策**：Electron（2026-08-03 用户拍板，取代此前 PySide6 定稿）。理由：① UI 能力最强（HTML/CSS/JS 渲染 Markdown/diff 有现成生态）；② WebSocket 协议语言无关，Node 原生 `ws` 库直连 daemon，无需 Python 桥；③ electron-builder 跨平台打包成熟（.dmg/.exe/.AppImage）。代价：体积 ~80-200MB（含 Chromium）、内存 ~200-500MB/实例——GUI 作为非开发者主入口，接受。Phase 2 的 daemon_manager.py 保留为**协议参考实现**，Node 薄客户端（daemon_client.js）照它写，行为一致。

**GUI 与打包的相互作用**：

1. **入口发现**：`emrg-gui` 与 `emrg` 同目录，启动时复用 `_start_daemon_background` 逻辑拉起 `emrgd`（`sys.frozen` 分支），然后以普通客户端身份连接——**GUI 不内嵌 daemon，不 fork 进程，只连 IPC**。
2. **平台表现**：
   - macOS：pkg 安装后额外生成 `.app` 外壳（Info.plist + 图标），点击启动台图标 = 运行 `emrg-gui`；TUI 仍可在终端用 `emrg`；
   - Windows：开始菜单/桌面快捷方式指向 `emrg-gui.exe`；终端用户用 `emrg`；
   - Linux：AppImage 本身即 GUI 容器（.desktop 文件内 Exec=emrg-gui），终端用 `emrg`。
3. **图标与资源**：`packaging/assets/` 统一维护图标（icns/ico/png 多尺寸），安装器与 AppImage 共用。
4. **多客户端会话协调**：TUI 与 GUI 同时操作同一 session 时，历史文件以 daemon 为唯一写者（现状即如此），客户端只读历史、写操作经 IPC 提交——无竞争风险。

**对打包的影响**：
- electron-builder 打包 `emrg-gui`（替代 PyInstaller 的 GUI 入口；TUI/daemon 仍用 PyInstaller）；
- GUI 技术栈（Electron）体积增大 80-200MB，风险表更新；
- 冒烟测试增加"GUI 启动 → 连接 daemon → 发消息 → 收到流式回复"用例。

### 7.1 极简 GUI 设计（最佳实践基准 + 功能极简）

> 原则：**GUI 第一版只做"够用"，把架构做对**。功能裁剪到最少，但通信、线程、状态管理、打包四件事一次做对，后续加功能只是加控件，不推翻架构。

#### 功能清单（v1 极简，明确砍掉什么）

| ✅ 做 | ❌ 明确不做（v2+） |
|-------|-------------------|
| 聊天：输入 → daemon → 流式显示 | Markdown 渲染（纯文本 + 简单加粗/代码段）→ v2 引富文本 |
| 会话：列表切换 / 新建 / 删除 | 重命名 / 搜索 / 归档 |
| 设置对话框：API key / base_url / model | 多模型切换器 / 代理 / 高级参数 |
| 状态栏：daemon 状态 / 模型名 / 演化计数 | 会话记忆浏览 / /memory |
| 工具调用显示为一行状态（`🔧 bash …`） | 工具卡片 / diff 视图 / 展开折叠 |
| 图片：粘贴 → 显示"[图片已接收] 文本占位" | 图片内容识别（vision 放 TUI） |
| ESC/中断按钮：停止当前响应 | 命令补全 `/`、快捷键体系 |

> 设计意图：**TUI 是"功能完整、键盘驱动、开发者向"；GUI 是"零学习成本、鼠标驱动、非开发者向"**。GUI v1 不重复 TUI 的完整功能矩阵，而是覆盖 80% 日常使用（聊天 + 会话 + 工具状态），把 Markdown 渲染、图片、diff 留给 v2 按需生长（对齐 MANIFESTO 第十四条"机制在需求真实出现时才生长"）。

#### 技术栈（具体到库）

| 项 | 选型 | 理由 |
|----|------|------|
| GUI 框架 | **Electron**（main 进程 Node + renderer Chromium） | UI 能力最强（HTML/CSS/JS 生态）；协议是 WebSocket，Node 原生 `ws` 库直连 daemon，语言无关 |
| 事件循环 | Node 事件循环（main）+ 浏览器事件循环（renderer） | main 进程 Node ws 客户端异步连 daemon；renderer 只渲染，经 IPC 通信 |
| 渲染 | React 或原生 JS + marked（Markdown）+ highlight.js（代码高亮） | 现成生态，Markdown/diff 渲染质量远超 QtWidgets |
| 配置 | renderer 读 `~/.emrg/config.toml`（main 代理 fs 访问） | 与 daemon 共享同一配置；改后 daemon 靠现有 mtime 检测自动重启 |

#### 代码结构（`emrg/gui/`，约 8 个文件）

```
emrg/gui/
├── package.json          # Electron 入口 + 依赖（ws / marked / highlight.js）
├── main.js               # main 进程：创建窗口、拉起 emrgd（sys.frozen 分支）、daemon 连接管理
├── preload.js            # contextBridge：renderer ↔ main 的 IPC 桥（安全沙箱）
├── renderer/
│   ├── index.html        # 主布局：左侧会话栏 + 右侧聊天区 + 底部输入条 + 状态栏
│   ├── app.js            # UI 逻辑：聊天渲染、会话列表、工具状态
│   ├── markdown.js       # marked + highlight.js 封装（流式增量渲染）
│   └── settings.js       # 设置对话框（config.toml 读写，经 preload IPC）
└── daemon_client.js      # Node ws 客户端：读 port 文件 + auth + 消息收发（协议参考 daemon_manager.py）
```

#### 复用层（关键设计：协议一致，Node 薄客户端）

`daemon_client.js` 是唯一与 daemon 通信的模块（main 进程内）。**Python 的 `daemon_manager.py` 不可被 Electron 直接 import**——但它是协议的**参考实现**，Node 客户端照它写，行为一致：

```
emrg/client/daemon_manager.py   # Python 参考实现（Phase 2 已实施，TUI 用）
  ├── ensure_connected()         # 拉起 + 建连 + auth（协议：读 port 文件 → ws://127.0.0.1:port → auth 首帧 → auth_ok）
  ├── send_task / send_command   # 消息封装（JSON type + params，ensure_ascii=False）
  ├── recv / read_stream         # 读流：yield (delta|tool_start|tool_end|done)
  └── ConnectionClosed 传播       # 断连检测（同 R11 语义）

emrg/gui/daemon_client.js       # Node 薄客户端（main 进程）：
  ├── ensureDaemon()              # 拉起 emrgd（spawn python -m emrg.server，sys.frozen 分支启动同目录二进制）
  ├── connect()                   # 读 ~/.emrg/emrgd.port → ws 连接 → auth 首帧 → auth_ok
  ├── sendTask(sessionId, prompt, images?)   # type="task"
  ├── sendCommand(type, params)   # ping/list_*/set_*/rant/...
  └── onEvent(cb)                 # 事件回调 → IPC 转发 renderer（message_delta / tool_started / tool_finished / done）
```

> **为什么 TUI 也要改（已由 Phase 2 完成）**：`client/app.py` 从 1994 行瘦身至 1796 行，协议客户端逻辑沉淀为 `daemon_manager.py`——它是 Node 客户端的行为参照。**Node 不复制 Python 代码，但复制协议语义**（相同 JSON 消息、相同 auth 流程、相同断连处理）。

#### 线程模型（双进程，IPC 桥）

```
Electron main 进程（Node）          Electron renderer 进程（Chromium）
  ├── daemon_client.js：ws 连接      ├── UI 渲染（React/原生 JS）
  ├── 事件循环：异步收 daemon 帧      ├── 用户输入 → IPC → main → daemon
  └── 事件 → IPC → renderer          └── IPC ← main ← daemon 帧 → 增量渲染
```

- main 进程是**唯一连 daemon 的进程**（daemon_client.js），renderer 零网络权限（contextBridge 隔离，安全沙箱）。
- 断线/daemon 崩溃 → main 检测 ConnectionClosed → 状态栏变红 + 自动重连（复用"未运行则拉起"逻辑）。
- 发送时 UI 不阻塞：输入条 disable + 状态栏"思考中…"，收到 done 后恢复。

#### 会话数据流（复用 daemon 已有 IPC）

```
启动 → main 进程 daemon_client.js 确保 emrgd 运行（spawn python -m emrg.server）
     → list_sessions?session_id=… → 左侧会话列表（无会话则自动新建）
发消息 → sendTask(sessionId, prompt, stream=true)（renderer → IPC → main → daemon）
     → 流式帧 → main onEvent → IPC → renderer 增量追加
     → tool_start/tool_end → 状态栏一行「🔧 bash — 运行中… / 完成 1.2s」
切换会话 → resume_session → chat_view 加载历史（daemon 返回 history_list）
```

会话历史、记忆、compact 全部由 daemon 管理——GUI 重启后从 daemon 恢复，**本地零状态**（对齐 MANIFESTO 服务端/客户端架构条款）。

#### 极简 UI 布局（ASCII 示意）

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

#### 首启引导（极简 GUI 的"最佳实践"细节）

1. 首次启动 → 检测 `~/.emrg/config.toml` 无 api_key → 自动弹出设置对话框；
2. 填 api_key → 写 config.toml → daemon 自动重启（mtime 检测）→ 状态栏变绿；
3. 未填直接进主界面也可用（聊天会报错并提示去设置）——不阻塞探索。

#### 验收标准（GUI v1）

- [ ] 全新环境：双击安装 → 打开 GUI → 首启引导填 key → 聊天 → 工具调用状态显示 → 正常
- [ ] 流式响应无卡顿（renderer 增量渲染不阻塞）
- [ ] 会话切换/新建/删除 与 TUI 操作同一 daemon，数据一致
- [ ] daemon 被杀 → 状态栏变红 → 自动拉起 → 重连成功
- [ ] `emrg`（TUI）与 `emrg-gui` 同时连接，互不干扰
- [ ] electron-builder 打包后 GUI 可运行（win/mac/linux）

---

## 8. 远程连接（WebSocket 协议的自然延伸）

> 现状：本机 IPC（UDS/Named Pipe）。
> **前提**：协议已统一为 WebSocket（见 [`roadmap.md`](roadmap.md) Phase 1）——本机 `ws://127.0.0.1:<port>`、远程 `wss://`，JSON 消息层不变。
> 未来：**远程连接**（客户端在笔记本，daemon 跑在服务器/家庭主机）。
> 原则：**协议层（JSON 消息）完全不动，远程只是给已统一的 WebSocket 协议加上 TLS + 认证**。

### 8.1 协议统一后的传输层

> 协议 WebSocket 化之后（roadmap Phase 1），`framing.py` 删除（业务改用 WS 原生 API）、`connect.py` 由 `websockets` 库统一替代为 TCP loopback：本机 `ws://127.0.0.1:<port>`（动态端口 + token）、远程 `wss://host:8743`。
> 本节（远程）构建在这一层之上，不再重复传输抽象细节——详见 roadmap。

远程与本地在协议上是**同一个 WebSocket 协议**，差异只在：

| | 本机 | 远程 |
|--|------|------|
| URL | `ws://127.0.0.1:<port>` | `wss://host:8743` |
| 加密 | 无（UDS 权限即边界） | **TLS 强制** |
| 认证 | 无（文件权限即认证） | token（+可选操作白名单） |
| daemon 管理 | 自动拉起/重启 | 不拉起/不重启，提示手动 |

### 8.2 远程传输选型（先厘清"谁负责加密"）

**ws vs wss 之争的本质不是协议，是"谁负责加密"。** 加密是可以分层的——不需要每一层都加密，但必须有一层负责：

| 路径 | 谁加密 | 传输内协议 | 安全前提 |
|------|--------|-----------|----------|
| **A. SSH 隧道** | SSH | **ws 明文均可** | SSH 已认证+加密，隧道内是可信通道（等价于本机 IPC）——加密在隧道层，内层不需要重复 |
| **B. 直连公网** | TLS | **wss** | TLS 强制；证书=自签名 + TOFU 指纹验证（见 8.3） |

**裸 ws 明文直连公网不可取**——不是"协议不好"，是威胁模型不允许：
- token 在连接首帧携带，**明文可重放**：偷听一次 = 永久拿着 token 冒充客户端；
- 而 daemon 的工具执行是 **RCE 级能力**（bash/read/write），风险等级极高；
- 相比之下 TLS 的成本≈0（一次握手 + 现代 CPU 可忽略的加解密），**没有理由省掉这一层**。

**设计**：
- **远程 = `wss://`（TLS）** + 自签名证书 TOFU 指纹验证 + token 认证（零 CA，见 8.3）；
- **SSH 隧道是官方零代码路径**——用户已有 SSH 访问权时，`ssh -L 8743:localhost:8743 server` 即可，隧道内走 `ws://localhost:8743` 明文，**零额外代码**；
- 浏览器客户端（未来）天然复用 `wss://`——协议统一的价值在此：TUI / GUI / 未来 web 客户端共享同一协议。

### 8.3 安全边界（远程是安全敏感场景）

远程暴露 daemon = 暴露 bash/read/write 工具 + LLM API key，**必须默认拒绝、显式开启**：

1. **默认关闭**：config 不配置 `[transport] remote` 即纯本机模式，行为与今天完全一致。
2. **直连模式 TLS 强制**：明文传输 API key、代码、token 不可接受。
3. **服务端身份验证——不用 CA，用 SSH 模式（TOFU）**：
   - 服务端自签名证书（`emrg remote enable` 一键生成，含私钥）；
   - 客户端首次连接显示证书指纹（`sha256:…`），**用户确认后写入** `~/.emrg/known_hosts`（或配置里手动钉死 `server_fingerprint`）；
   - 之后每次连接比对指纹，变化则警告并拒绝（防 MITM）；
   - **为什么不用 CA**：单用户自有主机场景，CA 签发链是纯负担——SSH 三十年证明了"自签名 + 首次确认"足够。
4. **认证（身份）**：共享 token（`emrg remote enable` 自动生成，首帧携带）。token 可重放，所以**必须**配加密层（第 2 条）——两者缺一不可。
5. **操作门控（可选，默认宽松）**：远程模式下可配置白名单（如仅允许聊天、禁用 bash），对齐 MANIFESTO 宿主授权精神；默认与本地一致（信任凭据持有者）。
6. **端口**：默认 8743（非特权），可配置。

### 8.4 客户端配置（config.toml）

```toml
[transport]
mode = "local"            # local（默认）| remote
# 以下仅 remote 模式生效：
# host = "my-server.example.com"
# port = 8743
# token = "…"                       # emrg remote enable 生成
# server_fingerprint = "sha256:…"   # 自签名证书指纹（首次连接确认后写入，或手动钉死）
# 或 server_cert = "~/.emrg/server.pem"  # 直接信任该自签名证书（等效信任锚）
# scheme = "wss"                    # wss（默认）| ws（仅 SSH 隧道内使用）
```

GUI/TUI 共享同一 config——远程连接对两种客户端同时生效，无需分叉。

### 8.5 远程模式下的行为差异（客户端侧）

| 行为 | 本地 | 远程 |
|------|------|------|
| daemon 未运行 | 自动拉起 | **不自动拉起**（远端可能没有 emrg 安装）→ 提示连接失败/配置错误 |
| mtime 变更自动重启 | ✅ | ❌（不能重启远端进程）→ 提示"远端 daemon 需手动更新" |
| 会话/记忆位置 | 本地 `<cwd>/.emrg/` | **远端** `<cwd>/.emrg/`（跟随 daemon 所在机器） |
| `emrg update` | 更新本地 | 更新本地客户端二进制；远端 daemon 由远端自行更新 |
| 工具执行（bash 等） | 本地 | **远端**（cwd 是远端路径）——这是远程的核心价值：远端有完整环境 |

> 重要语义：远程模式下，**会话、记忆、工具执行全部发生在 daemon 所在机器**。客户端只是瘦终端。这与 MANIFESTO"服务端是生命本体，客户端只是入口"完全一致——远程连接是这个架构的自然推论，而非特例。

### 8.6 与打包的关系

- 远程是**传输层特性**，不改变安装包结构（零新增捆绑——TLS 走 stdlib）；
- daemon 侧：`emrgd` 增加 `--listen remote` 启动参数（或 config 驱动），监听 `wss://`（TLS）而非本机 `ws://127.0.0.1:<port>`；
- 冒烟测试增加：本机起 daemon（remote 模式）→ 本机客户端以 remote 配置连接 → 全功能验证（TLS + token 认证）。

---

## 分发与托管

**结论：不需要任何额外服务器，全部托管在 GitHub Releases。**

| 分发项 | 托管位置 | 成本 |
|--------|----------|------|
| 平台安装包（.pkg / .exe / .AppImage） | GitHub Releases（本仓库 `releases/latest` 的附件） | 免费（单文件 ≤ 2GB） |
| 版本元数据（最新版本号、SHA256、下载 URL） | `https://api.github.com/repos/argszero/emrg/releases/latest` | 免费 |
| 下载统计 | GitHub API `download_count` 字段 | 免费 |
| 更新检查 | `emrg update` 直接查 Releases API | 免费 |

**下载入口**：

```
安装： 浏览器访问 github.com/argszero/emrg/releases/latest
        → 下载 EMRG-<v>.pkg / EMRG-Setup-<v>.exe / EMRG-<v>-linux-<arch>.AppImage
        → 双击安装（Linux: chmod +x 后运行）
更新：  emrg update（自动下载新版本，见 §5）
卸载：  emrg uninstall 或平台卸载器（见 §6）
```

> **旧的在线 install.sh 废弃**。仓库根目录的 `install.sh` 将删除，README 不再宣传 `curl | bash`。所有用户（无论是否开发者）走同一条路径：下载对应平台的安装文件。贡献者从 GitHub 克隆源码参与开发，但**安装**只有一种方式。

**为什么不引入 CDN/对象存储**：
- GitHub Releases 对开源项目免费且无限速（单文件上限 2GB，本包 <100MB 远低于限制）；
- 与源码、CI、Issues 同处一个平台，发布流程（打 tag → Actions 构建 → 自动 attach）零额外配置；
- 需要自建镜像时（国内下载慢、企业内网），后续可加"镜像源"配置项：`~/.emrg/config.toml` 中 `[update] mirror_url`，默认指向 GitHub，用户可切换。**此项列为 Phase 4 可选项，不阻塞主流程**。

---

## 构建流水线（GitHub Actions）

```
workflow: build-release.yml
trigger: tag v* 推送 / workflow_dispatch

matrix:
  - os: macos-15        → 产物: EMRG-<v>.pkg
  - os: macos-13        → 产物: EMRG-<v>.pkg（Intel，若仍需要）
  - os: ubuntu-24.04    → 产物: EMRG-<v>-linux-x86_64.AppImage
  - os: ubuntu-24.04-arm → 产物: EMRG-<v>-linux-aarch64.AppImage
  - os: windows-2025    → 产物: EMRG-Setup-<v>.exe

每步:
  1. uv sync --frozen
  2. uv run pytest tests/            # 回归
  3. pyinstaller emrg.spec           # 双入口 + datas
  4. 捆绑 git（下载便携构建）+ gh（官方 release 二进制）→ bin/
  5. 平台包装（pkgbuild / innosetup / AppImage / tar.gz 兜底）
  6. 上传 artifact + 附加到 release
```

**PyInstaller spec 要点**（`packaging/emrg.spec`）：
- `Analyze(['emrg/__main__.py', 'emrg/server/__main__.py'])` → 两个 EXE（emrg、emrgd）；**emrg-gui 用 electron-builder 单独打包**（不走 PyInstaller）
- `datas`：上述 5 个模板文件 + LICENSE + `packaging/assets/`（图标）
- `hiddenimports`：`yaml`（C 扩展）、`jinja2`、`httpx`、`rich`、skills loader 的动态 import 模块
- `--noupx`（规避杀软误报）
- onedir 模式（daemon 长期运行 + 更新替换需要，避免 onefile 的临时解压目录与运行中文件替换冲突）

**签名与公证**（凭据注入 CI secrets）：
- macOS：Developer ID Application 签名 → `notarytool` 公证 → `stapler` 装订。无证书时 CI 跳过签名，产物标注 `unsigned`，文档说明右键打开方式。
- Windows：EV 代码签名证书（可选）。未签名时 SmartScreen 会有提示，属预期。
- Linux：无强制签名；tarball 附 SHA256 校验。

---

## 分阶段实施计划

### Phase 1 — 打包跑通（本地）
- [ ] `packaging/emrg.spec` 双入口构建，本地产出可用 tarball
- [ ] `sys.frozen` 分支：`_start_daemon_background` 启动 `emrgd`；`emrg update` 走自更新分支
- [ ] 从 `client/app.py` 提取 `client/daemon_manager.py`（daemon 管理 + 协议读写，TUI/GUI 共用）
- [ ] git/gh 解析器：优先捆绑版、兜底系统版，探测结果写入 `install-info.json`
- [ ] 全量审计运行时写入路径，消除安装目录写入（promote/open-source 状态文件迁移）
- [ ] 冒烟测试：全新 macOS 容器（无 python/uv/git）解压 → 启动 daemon → 聊天 + 工具调用 + 会话持久化 + 演化任务跑通
- [ ] `scripts/build.sh` 本地构建脚本

**验收**：干净的 macOS 上，解压即用，`/help`、工具调用、`/rant`、演化周期全部正常。

### Phase 2 — CI 发布矩阵
- [ ] GitHub Actions matrix 构建（3 平台 × 架构）
- [ ] release 自动化：打 tag → 自动构建 + 附加资产 + 生成 SHA256
- [ ] 打包冒烟测试纳入 CI（在无 Python 的 runner 镜像上跑 `emrg --version` + `emrg server stop/restart` + 演化干跑）

### Phase 3 — 平台安装器 + 卸载
- [ ] macOS `.pkg`（pkgbuild + productbuild，安装向导 + 卸载器）；有证书则签名公证
- [ ] Windows Inno Setup `.exe`（安装向导 + unins000.exe + PATH + 快捷方式）
- [ ] Linux AppImage（linuxdeploy 封装，首次运行建 `~/.local/bin/emrg` 符号链接）+ tarball 兜底
- [ ] `emrg uninstall` 全流程实现（终止报告 + 墓地快照 + 清理 + 自校验）
- [ ] README 安装/卸载章节重写：分平台"下载 → 点击安装/卸载"指引
- [ ] 删除根目录 install.sh

### Phase 4 — 自动更新闭环
- [ ] `emrg update` 自更新实现（版本目录 + 符号链接 + 原子切换 + 回滚）
- [ ] 更新检查提示（启动时比对最新 release 版本，可选开关）

### Phase 5 — GUI 客户端（极简 v1，并行推进，不阻塞 1-4）
- [ ] 技术栈定稿：Electron + Node `ws` 客户端，验证 electron-builder 打包
- [ ] `emrg/gui/` 按 §7.1 结构落地：main.js / preload.js / renderer（app.js + markdown.js + settings.js）/ daemon_client.js
- [ ] 协议接入：daemon_client.js 照 daemon_manager.py 写（读 port 文件 + auth + 消息收发），行为一致
- [ ] 功能 v1 验收：聊天流式 + 会话切换/新建/删除 + 工具状态行 + 设置对话框（首启引导填 key）
- [ ] `emrg-gui` 入口接入 daemon 启动逻辑（spawn python -m emrg.server，sys.frozen 分支）
- [ ] 平台外壳：macOS `.app`（Info.plist+图标）、Windows 快捷方式、Linux AppImage `.desktop`（Exec=emrg-gui）
- [ ] GUI 冒烟测试：启动 → 连 daemon → 流式对话 + 工具调用 → daemon 被杀自动重连
- [ ] 多客户端同开验证：TUI + GUI 同时连接，会话/记忆一致
- [ ] v2 立项清单（不实现）：diff 视图、图片 vision、/命令补全、记忆浏览（Markdown 渲染用 marked 已含）

### Phase 6 — 远程连接（wss + 认证，协议已在 Phase 1 统一）
- [ ] 前提：协议 WebSocket 化已完成（roadmap Phase 1）——本机 `ws://127.0.0.1:<port>` + token 已就绪
- [ ] remote 实现：`wss://`（websockets + ssl）+ token 认证
- [ ] `emrg remote enable`（服务端）：一键生成自签名证书 + token + 打印指纹
- [ ] `emrg remote connect`（客户端）：首次连接 TOFU 确认指纹 → 写入 known_hosts
- [ ] config `[transport]` 支持：mode/host/port/token/server_fingerprint（或 server_cert）
- [ ] 客户端行为分支：远程不自动拉起/不重启 daemon、提示手动更新
- [ ] `emrgd --listen remote` 启动参数
- [ ] 安全验收：无 TLS 拒绝启动、token 错误拒绝连接、指纹变更拒绝连接、明文抓包无敏感数据
- [ ] SSH 隧道官方文档路径（`ssh -L` 桥接，隧道内 ws 明文，零代码）
- [ ] 远程冒烟测试：本机 remote 模式全功能验证（聊天 + 工具 + 会话）

---

## 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| PyInstaller 漏收动态 import（skills loader 用 importlib 按路径加载） | 运行时 ModuleNotFound | spec 中显式 hiddenimports；冒烟测试覆盖 `/skills` |
| 打包体积 ~60-90MB/平台（含 git/gh） | 下载变慢 | 接受（用户换取零依赖）；tarball 用 gzip；后续评估 Nuitka 压缩 |
| 捆绑 git/gh 的许可证/分发合规 | 法律风险 | git: GPL-2.0（独立可执行，非链接，合规）；gh: MIT。随包附带各自 LICENSE 文件 |
| Windows 杀软误报（PyInstaller 常见） | 安装被拦截 | `--noupx`；代码签名证书；提交杀软白名单申诉 |
| 无 Apple 证书则 macOS Gatekeeper 拦截 | 首次运行需右键打开 | 文档说明；社区版标注；有账号后启用公证 |
| 安装目录被误改 | daemon 崩溃 | 安装目录只读 + 运行时零写入审计；升级用版本目录隔离 |
| 测试套件（pytest）不打包 | 打包产物无回归保障 | CI 中先跑全量 pytest 再打包；产物跑冒烟脚本 |
| 捆绑 git 构建失败（某平台无便携版） | 该平台安装不完整 | CI 构建时验证捆绑产物存在；缺失则构建失败（fail-fast），不发布残缺包 |
| 卸载误删宿主数据 | 不可逆损失 | 卸载只删 `~/.emrg/`；`<cwd>/.emrg/` 项目数据仅列位置不删除；删除前强制墓地快照 |
| GUI 框架（Electron）体积 +80-200MB | 下载变大、安装变慢 | 接受（GUI 是主入口，体验优先）；Chromium 自带（无额外系统依赖） |
| Linux 上 GUI 依赖 | 违背零依赖目标 | Electron 捆绑 Chromium，规避系统 WebKit 依赖 |
| GUI 与 TUI 同时操作同一会话 | 状态不一致 | daemon 为唯一写者（现状），客户端只读历史、写操作经 IPC 提交 |
| 两个 PyInstaller 入口（emrg/emrgd）+ electron-builder（emrg-gui）打包互扰 | 产物残缺 | 各自独立打包；冒烟测试逐个验证入口 |
| Electron main 进程 Node ws 断连检测遗漏 | 断连不重连 | daemon_client.js 监听 ws close/error（同 TUI 的 ConnectionClosed 语义）；状态栏变红 + 自动重连 |
| 从 app.py 提取 daemon_manager 引入 TUI 回归 | TUI 功能受损 | 提取后 TUI 全量 pytest + 手动冒烟；共享逻辑用独立单测覆盖 |
| 远程模式暴露 bash/API key（安全敏感） | 凭据与代码泄漏 | 默认关闭 + 直连 TLS 强制 + token + TOFU 指纹；安全验收含抓包无明文 |
| TOFU 首次确认被用户跳过 → MITM | 中间人接管连接 | 首次连接强提示（非交互环境则拒绝连接）；支持 `server_fingerprint` 手动钉死；指纹变更即拒连 |
| 远程 daemon 版本与本地客户端不匹配 | 协议/行为错乱 | 连接时交换版本号（ping 响应已有 `started_at`/`model`，补 `version`）；不匹配则警告 |

---

## 决策记录

1. **打包工具**：PyInstaller（方案 A），Nuitka 为 Phase 2 评估项。
2. **统一安装方式**：每平台一个安装文件（macOS .pkg / Windows .exe / Linux AppImage），开发者与非开发者无差别；旧的在线 install.sh 删除，无任何安装脚本。
3. **捆绑一切**：Python + 第三方库 + git + gh 全部入包，安装即完整；gh 认证仍由用户 `gh auth login`（不可自动化）。
4. **用户级安装**：macOS/Windows 装到 `~/.emrg/install/`（Windows: `%LOCALAPPDATA%\EMRG\`）；Linux 为 AppImage 单文件 + `~/.local/bin` 符号链接。全程免 sudo，三平台行为一致。
5. **双二进制**：`emrg` + `emrgd`，onedir 模式，`sys.frozen` 分支兼容源码/打包两种运行方式。
6. **自更新**：版本目录 + 符号链接原子切换，失败回滚，免 sudo，只动安装目录不动数据。
7. **卸载全面**：`emrg uninstall` + 平台卸载器，终止报告 + 墓地快照 + 清理运行时 + 环境痕迹 + 自校验，幂等可重跑。
8. **数据与安装目录严格分离**：安装目录只读，一切可变数据在 `~/.emrg/` 与工作目录 `.emrg/`。
9. **TUI + GUI 双客户端**：`emrg`（TUI）与 `emrg-gui`（GUI）是平等入口，共享唯一 `emrgd`；协议（WebSocket）复用，GUI 零状态只渲染。
10. **GUI v1 极简（2026-08-03 改版：Electron）**：Electron（main Node + renderer Chromium）+ Node `ws` 客户端（daemon_client.js，照 daemon_manager.py 写）；功能只做聊天/会话/工具状态行/设置，diff/图片/补全留 v2；**Phase 2 的 daemon_manager.py 是协议参考实现，Node 薄客户端复制协议语义不复制 Python 代码**。
11. **远程连接 = WebSocket 协议的自然延伸**：协议统一（roadmap Phase 1，本机 `ws://127.0.0.1:<port>`、远程 `wss://`）后，远程只加 TLS + token + **自签名证书 TOFU 指纹验证（SSH known_hosts 模式，无 CA）**；SSH 隧道为官方零代码路径（隧道内 ws 明文——加密分层，不叠加）。默认关闭、直连 TLS 强制。远程模式下会话/记忆/工具执行全在 daemon 侧，客户端是瘦终端（MANIFESTO 架构条款的自然推论）。
