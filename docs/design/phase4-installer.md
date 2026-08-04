# Phase 4 设计：一键安装包（standalone python + 源码，干净机器双击安装 → GUI + TUI 全可用）

> 主路线图：[`roadmap.md`](roadmap.md) Phase 4（安装文件分发）
> 前置依赖：Phase 1 协议 WebSocket 化 ✅、Phase 2 daemon_manager ✅、Phase 3 Electron GUI ✅（见 [`phase3-electron-gui.md`](phase3-electron-gui.md)，已实施 PR #321-359）
> **方案变更（2026-08-04，用户拍板）**：放弃 PyInstaller，改用 **uv standalone python + 源码**（方案 C）。理由见 §1.4。本文取代 `packaged-installer.md` 的分阶段实施计划与 `phase4-installer.md` 旧版（PyInstaller 版）。
> 修订：v2（方案 C）

---

## 1. 目标与验收（一步完成）

**唯一目标**：干净机器（无 python / uv / git / gh / node）→ 下载一个安装文件 → 双击安装 → GUI（emrg-gui）与 TUI（emrg）全部可用，演化系统可用，**会话内可执行 python 脚本**。

### 1.1 平台交付物

| 平台 | 安装文件 | 安装体验 | 安装位置 |
|------|----------|----------|----------|
| macOS | `EMRG-<ver>.pkg` | 双击 → 安装向导 → 完成；启动台出现 `EMRG.app`；终端可用 `emrg` | `~/.emrg/install/`（用户级，免 sudo）+ PATH |
| Windows | `EMRG-Setup-<ver>.exe` | 双击 → 安装向导 → 完成；开始菜单出现 `EMRG` 快捷方式；终端可用 `emrg` | **`~/.emrg\install\`**（R34：统一三平台前缀，弃 %LOCALAPPDATA% 特例）+ PATH |
| Linux | `EMRG-<ver>-linux-<arch>.AppImage` | 下载 → chmod +x → 双击运行（GUI）；**首次运行自解压到 `~/.emrg/install/` + 建 `~/.local/bin/emrg` 启动器** | `~/.emrg/install/`（自解压）+ `~/.local/bin` |

### 1.2 验收标准（全部一次满足）

- [ ] 干净容器（无 python/uv/git/gh/node）→ 双击安装 → 无报错
- [ ] **全程离线（断网）安装 + 首启 + 聊天可用**（R47：安装包自包含、零在线安装）
- [ ] 安装后 GUI 启动 → 首启引导填 key → 聊天流式 + 工具调用 + 会话持久化
- [ ] 安装后 TUI `emrg` 启动 → `/help`、聊天、`/rant`、演化周期正常
- [ ] **会话内 `python script.py` 可执行**（bash 工具走捆绑 python）
- [ ] GUI 与 TUI 同开同 daemon，数据一致（广播模型）
- [ ] 平台卸载器彻底清理（终止报告 + 墓地快照 + install/ + 数据 + PATH/快捷方式 + 自校验），幂等
- [ ] 捆绑 git/gh 生效：干净机器上演化系统的 commit/PR 流程可用
- [ ] 安装目录零写入（一切可变数据在 `~/.emrg/` 与工作目录 `.emrg/`）

### 1.3 范围决策（一步交付，但明确不含）

| 含（本次实现） | 不含（后续） |
|---|---|
| standalone python + 源码部署 | 远程连接（Phase 5） |
| electron-builder 打包 GUI + emrgd 分发 | `emrg update` 二进制自更新（v1.1，本次只做提示） |
| git/gh 捆绑 + PATH 注入 | 代码签名公证（无证书标注 unsigned） |
| 三平台安装器（pkg / exe / AppImage）+ 卸载 | Nuitka / PyInstaller |
| CI build-release.yml 全自动 | |

### 1.4 为什么放弃 PyInstaller（方案 A → 方案 C）

| 维度 | A. PyInstaller（旧） | C. standalone + 源码（新） |
|------|:---:|:---:|
| 体积 | ~240MB（**3 套 python**：emrg onedir + emrgd onedir + 会话用 standalone） | ~250MB（**1 套 python**；R48 实测：python 60 + lib 20 + source 1 + git 40 + gh 30 + GUI 100） |
| 会话内跑 python 脚本 | ❌ 需另捆绑 standalone | ✅ **同一套 python 天然支持** |
| 模板收集 | ❌ spec datas 手动精确路径（prompt.md 在 server/ 根） | ✅ 源码原样 |
| frozen 分支 | ❌ 需改 `__main__.py` + GUI resourcesPath | ✅ 启动脚本即可 |
| 依赖重复（emrg/emrgd） | ❌ 各一份解释器+依赖 | ✅ 共享 |
| 更新 | 重新 PyInstaller 构建 | 替换 source/ + lib/（R72：lib/ 构建期预装——**运行时零 pip**，与 R47 一致；非"pip install"） |
| 启动速度 | 快（C bootloader） | 稍慢 ~300ms（可接受） |

**核心洞察**：PyInstaller 解决的"没 python 也能跑"——但我们**反正要捆绑 python 给会话内脚本用**（§5.0 已论证），那就直接用这一个 python 跑源码，PyInstaller 变成纯冗余。standalone python（uv 官方，含 pip）60MB、依赖 lib/ 20MB、源码 ~1MB，一套搞定。

**⚠️ 体积明细（R48 实测修正）**：方案 C 总包 ~250MB（macOS），与 PyInstaller 方案同量级——**方案 C 的价值不是省体积，是"一套 python 三用 + 免 spec/frozen 坑"**：

| 组件 | 体积 |
|------|------|
| standalone python（实测 60MB，含 pip） | 60MB |
| 依赖 lib/（R43 实测，含传递依赖+C 扩展） | ~20MB |
| emrg 源码（除 gui，R29） | ~1MB |
| git（便携版） | ~40MB |
| gh（Go 单文件） | ~30MB |
| GUI（electron-builder，R48） | ~100MB（macOS .app）/ ~80MB（Win）/ ~90MB（AppImage） |
| **合计** | **~250MB（macOS）/ ~230MB（Win）/ ~240MB（AppImage）** |

> 体积主要被 GUI（Chromium）与 git/gh 占——这是"安装即完整"的固有成本（PyInstaller 方案同量级）。**git/gh 仅演化系统用（R53 实证：git 42 处调用全在 daemon/scheduler/update，gh 在演化 prompt）**——普通聊天用户用不到，但演化是 MANIFESTO 核心，v1 全捆绑保证开箱即用；体积优化后续项：gh 可选装 / git 用系统版兜底（§5.2 解析器已支持）——v1 接受全捆绑。

---

## 2. 安装产物结构（三平台统一）

```
<prefix>/                         # 三平台统一：~/.emrg/install/（R34 弃 Windows LOCALAPPDATA 特例）；Linux AppImage 自解压
├── bin/
│   ├── python                   # ⭐ standalone CPython（uv 官方，含 pip，60MB 实测）——唯一一套 python
│   ├── python3                  # 符号链接
│   ├── emrg                     # 启动脚本：exec python -m emrg（R13：-m 包入口 + PYTHONPATH=source:lib）
│   ├── emrgd                    # 启动脚本：exec python -m emrg.server（同 R13）
│   └── gh                       # 捆绑 gh CLI（官方单文件二进制）
├── git/                         # ⚠️ R60：捆绑 git（Windows: Git for Windows portable 整目录——
│   │                            #     cmd/ + bin/ + mingw64/ + libexec/；macOS/Linux: 单二进制直接放 bin/）
│   ├── cmd/git.exe
│   ├── mingw64/bin/
│   └── ...
├── lib/                         # 依赖 site-packages（20MB，构建期 pip --target 装好打进安装包，R47）
├── source/                      # emrg 源码（~1MB，只读，安装器放置）
│   ├── emrg/
│   │   ├── __main__.py
│   │   ├── server/__main__.py
│   │   ├── server/prompts/system.j2
│   │   ├── server/evolution_prompt.md   # 4 个 prompt.md 在 server/ 根
│   │   └── ...
│   └── LICENSE
├── assets/                      # 图标（icns/ico/png，packaging/assets/）
├── LICENSE
└── version.txt
```

数据目录不变：`~/.emrg/`（config.toml、sessions、memory、logs、projects.yml、tasks.yml、rants.jsonl、versions/、saturation/、skills/）。

> **安装目录只读**：`source/` 与 `lib/` 安装后只读（升级用原子替换，见 §7）。可变数据全在 `~/.emrg/` 与工作目录 `.emrg/`。

### 2.1 启动脚本（bin/emrg、bin/emrgd）

```bash
#!/usr/bin/env bash
# bin/emrg — TUI 客户端 + CLI 入口
# ⚠️ 相对定位（AppImage 挂载到临时目录也可用）：脚本自身路径 → install 根
# ⚠️ R71（致命，实测）：BASH_SOURCE 在软链调用时 = 软链路径（~/.local/bin/emrg → 软链目录），
#    必须 readlink 循环解析真实路径——否则 Linux 自解压的 ~/.local/bin/emrg 启动直接坏
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do SOURCE="$(readlink "$SOURCE")"; done   # 循环解软链（相对路径需拼）
case "$SOURCE" in /*) ;; *) SOURCE="$(dirname "${BASH_SOURCE[0]}")/$SOURCE" ;; esac
DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
PREFIX="$(dirname "$DIR")"
export PATH="$DIR:$PATH"                    # python/git/gh 对子进程可见
export PYTHONPATH="$PREFIX/source:$PREFIX/lib:$PYTHONPATH"   # ⚠️ R13：必须含 source/（emrg 包父目录）
export PYTHONDONTWRITEBYTECODE=1            # ⚠️ R61：source/ 只读（§1.2 零写入）——禁止写 __pycache__
exec "$DIR/python" -m emrg "$@"             # ⚠️ R13：用 -m emrg（包入口），非 python source/emrg/__main__.py
```

```bash
#!/usr/bin/env bash
# bin/emrgd — daemon 入口（独立进程，长期运行）
# ⚠️ R71：同 emrg 的 readlink 循环（emrgd 也可能被软链调用）
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do SOURCE="$(readlink "$SOURCE")"; done
case "$SOURCE" in /*) ;; *) SOURCE="$(dirname "${BASH_SOURCE[0]}")/$SOURCE" ;; esac
DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
PREFIX="$(dirname "$DIR")"
export PATH="$DIR:$PATH"
export PYTHONPATH="$PREFIX/source:$PREFIX/lib:$PYTHONPATH"
export PYTHONDONTWRITEBYTECODE=1            # R61
exec "$DIR/python" -m emrg.server "$@"
```

- **⚠️ R13（关键，实测验证）**：`python source/emrg/__main__.py` 直接跑会 `ModuleNotFoundError: No module named 'emrg'`——因为 sys.path[0] = `source/emrg/`（脚本所在目录），而 `import emrg` 需要 `source/`（包父目录）在 path。**必须**：
  1. `PYTHONPATH` 含 `$PREFIX/source`（emrg 包父目录）
  2. 用 `-m emrg` / `-m emrg.server`（包入口，`__main__` 由包机制解析）——实测 `emrg 0.2.0` 正常输出
- **Windows 版**：`.cmd` 批处理同逻辑（`%~dp0` 定位；`set PATH=%~dp0;%PATH%`、`set PYTHONPATH=<prefix>\source;<prefix>\lib;%PYTHONPATH%`、`set PYTHONDONTWRITEBYTECODE=1`、`python -m emrg %*`）
- **PATH 导出**：启动脚本内 `export PATH="$DIR:$PATH"` 使 daemon 继承后，bash 工具子进程（`create_subprocess_shell`）能直接找到 `python`/`git`/`gh`——**这是会话内脚本能力的关键**（§5.0）。**GUI 场景成立（R28）**：GUI 由启动台点击（launchd 最小 PATH `/usr/bin:/bin:...`）启动 → spawn emrgd 用绝对路径（不依赖 PATH 找到它）→ emrgd 脚本运行时注入 PATH → daemon 的 bash 工具继承 → 链路完整

### 2.2 ⚠️ source/ 复制必须排除 emrg/gui（R14，448MB 陷阱）

`emrg/gui/` 是 emrg 包的子目录，但**含 node_modules 448MB**（`du -sh emrg/gui` 实测）；Python 运行时**零依赖它**（grep 实证：无 `import emrg.gui`）。

**build-runtime.sh 的 source/ 组装**（精确复制，勿整包拷贝）：
```
复制（必需）：
  emrg/__init__.py  emrg/__main__.py  emrg/config.py  emrg/connect.py  emrg/protocol.py  emrg/session.py  emrg/memory.py
  emrg/py.typed                       # PEP 561 类型标记（零成本）
  emrg/client/  emrg/server/  emrg/tools/  emrg/skills/          # 各子目录（排除 __pycache__）
  emrg/server/prompts/system.j2  emrg/server/*.md                # 模板（在 server/ 根 + prompts/）
  LICENSE
排除（勿复制）：
  emrg/gui/            # 448MB node_modules；GUI 由 electron-builder 独立打包
  emrg/**/__pycache__/
  emrg/**/*.pyc
```
**等价**：`rsync -a --exclude gui --exclude __pycache__ emrg/ dist/runtime/source/emrg/`（无 rsync 平台用 tar/cp 同规则）。

---

## 3. 依赖与 lib/ 预装

### 3.1 依赖清单（pyproject.toml 已定）

```
rich>=13.0.0 / httpx>=0.27.0 / pyyaml>=6.0 / jinja2>=3.0 / websockets>=17.0.1
```
全为纯 Python/轻依赖（pyyaml 的 C 加速走 wheel，standalone python 的 pip 直接装预编译 wheel，无需本机编译器）。

### 3.2 依赖部署：**构建期预装，安装器零安装步骤（R47）**

**⚠️ 决策（R47，用户质疑修正）**：方案 C **100% 离线、零在线安装**——安装包内不含任何"首次启动时在线下载"的环节。依赖 lib/ 在 **CI 构建期**装好并打进安装包，安装器只做**纯文件复制/解压**（最快、最可靠、无失败点）。

**构建期（CI build-runtime.sh）完成依赖安装**：
```
uv python install 3.13                          # standalone python（构建机下载一次）
"$PREFIX/bin/python" -m pip install --target dist/runtime/lib/ \
    rich httpx pyyaml jinja2 websockets          # ⚠️ 全量（含传递依赖），禁 --no-deps（R3）
# → lib/ 已含全部依赖（含 markupsafe 等 C 扩展，R43 实测加载成功）
# → dist/runtime/lib/ 整体打进安装包
```
- **安装器 = 纯文件复制**：把 `dist/runtime/`（python + source + lib + 脚本）解压到 `~/.emrg/install/`——**不跑 pip、不联网、无安装失败点**
- **首次启动 = 零安装步骤**：emrg/emrgd 启动脚本直接跑（PYTHONPATH 指向解压好的 lib/）——AppImage 自解压也是本地文件操作（R8），秒级
- **"双击安装即完整"的严格含义**：安装 = 文件复制（秒级完成），之后**永不联网**（除非用户主动 emrg update，v1.1）
- **C 扩展**：pyyaml `_yaml.so` / markupsafe C 扩展在构建期装好（R1/R43 实测 PYTHONPATH 加载成功）

### 3.3 依赖锁定与跨平台（构建期）

CI 用 `uv pip compile` 生成 `packaging/requirements.lock`（**含全部传递依赖**）→ build-runtime.sh 据此 `pip install --target`。**跨平台**：CI matrix 各平台各装各架构的 wheels（同架构构建，R11）——无需 wheels/ 目录（lib/ 已预装，不需要目标机跑 pip）。requirements.lock 提交 git 保证可复现。

### 3.4 预打包 vs 首次启动清单（R47，明确边界）

| 组件 | 构建期（CI）预打包 | 安装器 | 首次启动 |
|------|:---:|:---:|:---:|
| standalone python（含 pip） | ✅ 打进安装包 | 纯复制 | 零操作 |
| emrg 源码 source/ | ✅ 打进安装包 | 纯复制 | 零操作 |
| 依赖 lib/（含传递依赖+C 扩展） | ✅ pip --target 装好打进安装包 | 纯复制 | 零操作 |
| git / gh | ✅ 打进安装包 | 纯复制 | 零操作 |
| GUI（electron-builder 产物） | ✅ 打进安装包 | 纯复制 | 零操作 |
| 配置 config.toml | ❌ 不打包（首次启动引导生成，G71/G116） | — | 首启引导（用户填 key） |
| **在线下载** | **无** | **无** | **无** |

> **保证**：安装包是自包含的（除 config 引导外一切就绪）；安装 = 文件复制（秒级）；首次启动不联网、不跑 pip、无失败点。干净机器**离线**可用（验证项：冒烟 12）。

> **⚠️ R62（TUI 首启引导盲区）**：首启配置入口只有 **GUI**（settings 对话框填 key 写 config.toml）。验收含"TUI 聊天正常"，但干净机器**先跑 TUI** 时：`ensure_config()` 写占位 api_key（config.py:99-130）→ 聊天必然失败且**无任何引导提示**。修正：TUI 检测 config 中 api_key 为占位符（`sk-...` 或空）→ 打印明确提示 **"请先运行 EMRG GUI 完成首启配置（填 API key），或编辑 ~/.emrg/config.toml"** 后照常进入（占位 key 聊天失败是预期，提示即可）。列入 §11 实施清单代码改造。

---

## 4. GUI 打包（electron-builder）

### 4.1 现状（已就绪）

`emrg/gui/package.json` build 段已配置（Phase 3 产出）：
```json
"build": {
  "appId": "com.emrg.gui",
  "productName": "EMRG",
  "extraResources": [{ "from": "../dist/emrgd", "to": "emrgd" }],
  "target": ["dmg", "exe", "AppImage"],
  "icon": "../packaging/assets/",
  "mac": { "identity": null }
}
```

### 4.2 ⚠️ GUI spawn emrgd（简化：复用同目录安装的 emrgd，R22）

**R22 简化（关键）**：GUI 与 TUI 装在同一安装目录（`~/.emrg/install/`）——**GUI 无需内嵌 runtime 副本**，直接 spawn 安装目录的 `emrgd` 启动脚本（与 TUI 共享同一 daemon 版本）。extraResources **不再复制 runtime**（electron-builder 只打包 GUI 本体）。

**GUI spawn emrgd（daemon_client.js 改造）**：
```javascript
// ⚠️ daemon_client.js 是纯 Node 模块（无 electron import，G99 单测依赖此）
// isPackaged 由 main.js 传入（main.js 已持有 app，main.js:8）
// main.js 创建处（main.js:338）：client = new DaemonClient({ projectDir, logger, isPackaged: app.isPackaged })

// daemon_client.js 内：
_findDaemonExecutable() {
  if (this.isPackaged) {           // 由 main.js 注入
    // R22+R34：固定走安装目录（三平台统一 ~/.emrg/install/，与 TUI 共享同一 daemon 版本）
    const exe = process.platform === "win32" ? "emrgd.cmd" : "emrgd";  // R35：Windows 需 .cmd 扩展（spawn 找不到无扩展文件）
    return path.join(os.homedir(), ".emrg", "install", "bin", exe);
  }
  return null;  // 源码模式走 _findPython()
}
// startDaemon()：打包模式 spawn 安装目录 emrgd（无参数，脚本内部 exec python + 设 PATH/PYTHONPATH）
//               源码模式保持现状（.venv python -m emrg.server）
// ⚠️ R36：Windows spawn .cmd 需 shell 语义——spawn(emrgdPath, { shell: true, ... })（.cmd 非 PE 可执行，
//    CreateProcess 直接跑失败；shell:true 走 cmd.exe /c）；POSIX 直接 spawn 脚本（shebang 可执行）。
//    其余选项不变：cwd=projectDir（G125）/ stdio ignore（G68）/ detached / unref（shell 中间层不影响脱离）
// ⚠️ R66：Windows 打包分支加 windowsHide: true——detached + shell:true 默认开新控制台窗口（黑窗闪烁）；
//   cmd.exe /c 同步等待 python 退出 → GUI 退出后 cmd 仍在（detached 独立进程组）→ python 存活 → daemon 常驻 ✅
```
- 启动脚本自行定位 python/source/lib，GUI 只需 spawn 它
- cwd=projectDir（G125）/ stdio ignore（G68）/ detached / unref 选项不变
- **两套 runtime 不冲突（R22 实证）**：daemon pid 文件互斥（daemon.py:130-141）+ 共享 port 文件——先到先得，第二个自我退出；GUI 与 TUI 始终直连同一 daemon
- **AppImage 时序（R25+R64+R65）**：AppImage 是 GUI 容器——**runtime payload 通过 `build.linux.extraResources` 打进 AppImage**（R64：仅 linux 段携带，mac/win 不携带——R23"删除 extraResources"限定 mac/win）；**自解压逻辑归 GUI main.js**（R65：electron 启动时检查 `~/.emrg/install/bin/emrgd` 不存在 → `fs.cpSync(process.resourcesPath/runtime → ~/.emrg/install)`（本地复制秒级）→ 建 `~/.local/bin/emrg` 软链 → 再 spawn emrgd——**不覆盖 electron-builder 默认 AppRun**，因无官方自定义 AppRun 支持，覆盖 hack 脆弱）；pkg/exe 安装场景 install/ 恒存在，main.js 自解压分支不触发
- **electron-builder 配置变化（R23 实证 + R64 修正）**：`emrg/gui/package.json` build 段**删除顶层 `extraResources`**（现为 `[{from: '../dist/emrgd', to: 'emrgd'}]`——Phase 3 PyInstaller 遗留，R22 后 GUI 不再携带 runtime）——**但 Linux AppImage 需要 runtime 自解压源（R64）**，per-platform 配置：
```json
"build": {
  "appId": "com.emrg.gui",
  "productName": "EMRG",
  "target": ["dmg", "exe", "AppImage"],
  "icon": "../packaging/assets/",
  "mac": { "identity": null },
  "linux": {
    "extraResources": [{ "from": "../dist/runtime", "to": "runtime" }]
  }
}
```
（mac/win 无 runtime；linux 有——AppImage 内含 runtime 作自解压源，解压后 `~/.emrg/install/` 与 pkg/exe 布局一致）
- cwd=projectDir（G125）/ stdio ignore（G68）/ detached / unref 选项不变

### 4.3 构建顺序

```
构建 runtime（python + source + lib + emrgd 脚本）→ dist/runtime/（安装器用 + Linux AppImage 自解压源，R64）
cd emrg/gui && npm run dist   # electron-builder：mac/win 无 runtime；linux 段 extraResources 带 dist/runtime（R64）
平台包装：dist/runtime/ + GUI 产物组装进安装器（pkg/exe）；AppImage 已含 runtime（linux extraResources，R64）
```

### 4.4 GUI 平台放置（R33，按平台惯例）

GUI（electron-builder 产物）**不放 `<prefix>/bin/`**——按平台惯例放置，启动台/开始菜单可见：

| 平台 | GUI 位置 | 说明 |
|------|----------|------|
| macOS | `~/Applications/EMRG.app` | 用户级（免 sudo），启动台可见；pkg 安装时复制到 ~/Applications |
| Windows | `~/.emrg\install\emrg-gui\EMRG.exe` | 开始菜单快捷方式指向它（Inno Setup 创建） |
| Linux | AppImage 单文件本身 | 下载 → chmod +x → 双击即 GUI；桌面 .desktop 文件（首次运行创建） |

**GUI 与 daemon 的关系（R22 已定）**：GUI spawn `~/.emrg/install/bin/emrgd`（绝对路径，与 GUI 自身位置无关）——所以 GUI 放哪都不影响 daemon 拉起。

---

## 5. git/gh 捆绑 + PATH 注入

### 5.1 捆绑来源（CI 下载，随包分发）

| 平台 | git | gh |
|------|-----|-----|
| Windows | **Git for Windows portable（官方，自解压单文件 → 整目录解压入 `install/git/`）** | gh 官方 release 单文件二进制 |
| macOS | **⚠️ R59：git-scm 无官方便携二进制**（官方只提供 .dmg/.pkg 安装器）——CI **源码编译**（`./configure && make`，依赖 macOS 系统库 LibreSSL/zlib/iconv，产物跨 macOS 版本可移植） | 同上 |
| Linux | **⚠️ R59：官方只有源码**——CI 静态编译（musl 或静态链接 openssl/curl）；构建成本高则兜底 `resolve_git_gh` 的 which（系统 git，§5.2） | 同上 |

> **R59 风险确认**：git 便携二进制**没有官方现成下载源**（Windows 除外）——CI 需增加源码编译步骤（+3~5 分钟/平台）。这是"干净机器演化系统可用"验收的硬依赖：若编译失败则 CI fail-fast 不发残缺包（§10 已列原则）。gh 三平台均有官方二进制 ✅（Go 单文件，零依赖）。

随包附各自 LICENSE（git: GPL-2.0 独立可执行；gh: MIT）。

### 5.2 PATH 注入（主路径，覆盖三件套）

**启动脚本已 `export PATH="$DIR:$PATH"`（§2.1）**——`python`/`git`/`gh` 对 daemon 及 bash 工具子进程全部可见：

- **会话内 `python script.py`** ✅（§1.2 验收项——EMRG 与用户脚本共享同一 python）
- **git**：`["git", ...]` 裸调用自动命中捆绑 git（scheduler.py:164 / git_utils.py:15 / __main__.py:325 无需改——PATH 优先）
- **gh**：`which gh` 命中捆绑 gh
- **⚠️ R60（Windows git 目录）**：macOS/Linux git 单二进制放 `bin/` 已被 `$DIR` 覆盖；**Windows 的 Git for Windows portable 是整目录（§2 结构图 `install/git/`）**——`.cmd` 启动脚本的 PATH 注入需**额外包含 `%~dp0..\git\cmd` 与 `%~dp0..\git\mingw64\bin`**（git.exe 运行时按相对路径找自身运行库，但 `spawn("git", ...)` 需在 PATH 找到 `cmd/git.exe`）；`.cmd`：`set PATH=%~dp0;%~dp0..\git\cmd;%~dp0..\git\mingw64\bin;%PATH%`

**⚠️ AppImage 特例（R8 实测分析）**：
- AppImage 运行 = 挂载到 `/tmp/.mount_XXXX`（**每次随机**）→ 启动脚本的 PATH 注入指向**临时挂载目录**——运行期间可用，但：
  - `~/.local/bin/emrg` **不能是符号链接**（挂载路径每次变）→ 必须是**启动器脚本**：`exec <AppImage绝对路径>`（首次运行时把 AppImage 绝对路径写入启动器）
  - **长期进程（daemon 由 GUI 拉起）** 的 PATH 注入在 AppImage 退出后失效 → **首次运行必须把 python/git/gh 复制到 `~/.emrg/install/bin/`**（数据目录可写），启动器 exec AppImage 前先确保复制完成
- 因此 AppImage 的**首次运行自解压**是必须的：`bin/python`/`bin/git`/`bin/gh` → `~/.emrg/install/bin/`，source/lib → `~/.emrg/install/`（与 pkg/exe 安装后的布局一致）——**AppImage 实际是"自解压安装器"**，之后 PATH 注入走 `~/.emrg/install/bin/`（稳定路径，非临时挂载）
- **自解压实现（R56 方案 → R65 修正）**：**实现位置归 GUI main.js**（electron-builder 默认 AppRun 无官方自定义支持，覆盖 hack 脆弱）——main.js 启动时：`if (!fs.existsSync(join(os.homedir(),'.emrg','install','bin'))) { fs.cpSync(join(process.resourcesPath,'runtime'), join(os.homedir(),'.emrg','install'), {recursive:true}); fs.symlinkSync(join(os.homedir(),'.emrg','install','bin','emrg'), join(os.homedir(),'.local','bin','emrg'), 'file'); }`（`process.resourcesPath` = AppImage 挂载点 `usr/lib/emrg/resources/`，runtime 来自 `build.linux.extraResources`（R64）；本地复制秒级）→ 复制完 spawn emrgd → 之后 GUI/TUI 都从 `~/.emrg/install/` 跑（稳定路径，与 pkg/exe 一致）。AppRun 保持 electron-builder 默认（启动 GUI 本体）

**`resolve_git_gh()` 解析器（保留为兜底）**：
- 场景：安装不完整、用户自定义
- 实现：`~/.emrg/install/bin` 存在则优先，否则 `shutil.which()`；缓存 `~/.emrg/install-info.json`
- 模板注入 `{{ git_path }}`/`{{ gh_path }}`（scheduler `_build_evolution_prompt` context 加两键；jinja2.Undefined 已容忍缺失，无回归）
- gh 认证仍由用户 `gh auth login`（OAuth 不可自动化）

---

## 6. 卸载（平台卸载器为唯一官方入口）

**设计决策（2026-08-04 简化）**：普通用户卸载 = 平台卸载器（系统标准交互）。**不新增 `emrg uninstall` 命令**——终端用户可用 `rm -rf ~/.emrg`（开发者能力，非官方入口）。平台卸载器是独立进程，天然无"自删锁"问题（方案 C 的脚本入口不会有此坑）。

### 6.1 平台卸载器（各自标准交互）

| 平台 | 卸载方式 | 卸载器做的事 |
|------|----------|-------------|
| macOS | **自带卸载 app**（安装时放 Applications，双击运行；.pkg 无原生卸载器——R30） | 运行 `emrg-uninstall` 脚本（六步，§6.2）→ 提示拖入废纸篓（R31：运行中 .app 不能自删） |
| Windows | 控制面板卸载（Inno Setup 生成 unins000.exe） | **先关 GUI 进程（R73：GUI 在 `install/emrg-gui/` 内，运行中锁文件删不掉——taskkill 或检测提示关闭）** → `[UninstallRun]` 跑 `emrg-uninstall` 脚本（六步，§6.2）→ 原生删目录 + 快捷方式 |
| Linux | **运行卸载脚本 → 删 AppImage 文件** | ⚠️ R58：AppImage 首次运行已自解压 `~/.emrg/install/`（250MB，R56）——**删 AppImage ≠ 卸载**：须先运行 `~/.emrg/install/bin/emrg-uninstall`（六步，§6.2，含终止报告+墓地快照）→ 删 AppImage + `~/.local/bin/emrg` 软链 |

**macOS 卸载 app（R30+R31 补充）**：.pkg 安装**不生成卸载器**（macOS 无标准 pkg 卸载 API）——需 pkg 安装时额外放置一个"卸载 EMRG.app"（shell 脚本包装的 .app，双击运行调 `emrg-uninstall` 执行 §6.2 六步）。**自删限制（R31）**：运行中的 .app 不能删自己——卸载 app 删数据 + install/ 后，**提示"请将 EMRG 图标拖入废纸篓"**（macOS 用户习惯，不做延迟自删的复杂机制）。卸载 app 调用 python 需自设 PYTHONPATH（R15）——它本身是 bash 脚本，头部 `export PYTHONPATH=~/.emrg/install/source:~/.emrg/install/lib`。

### 6.2 终止报告 + 墓地快照（对齐 MANIFESTO 第十条【终止权】）

**⚠️ R58（统一实现）**：六步卸载逻辑**收敛为单一脚本 `~/.emrg/install/bin/emrg-uninstall`**（python，头部自设 PYTHONPATH=R15）——三平台卸载器统一调用它（macOS 卸载 app 双击执行 / Windows `[UninstallRun]` / Linux 终端运行），避免三处重复实现六步。

卸载器执行（幂等，任一步失败可重跑）：
```
1. 停止 daemon（现有 shutdown 协议，等待退出）
2. 生成终止报告 → ~/.emrg/logs/uninstall-report-<ts>.json
   （实例身份、演化次数、会话数、记忆数、终止原因=用户主动）
3. 留存经验墓地快照（不可删除项）：
   → 打包记忆 + 会话 + 演化日志 → ~/.emrg/graveyard/emrg-data-<ts>.tar.gz
   → 快照保留，卸载后用户可自行删除
4. 删除 ~/.emrg 全部（install/、versions/、config.toml、sessions、memory、
   logs、projects.yml、tasks.yml、rants.jsonl、saturation/、emrgd.sock/pid、install-info.json）
5. 清理环境痕迹：PATH（shell rc / 注册表）、Windows 快捷方式
6. 自校验：确认 emrg/emrgd/emrg-gui 已不可用、目录已删除，输出清理清单
```

**边界**：
- 宿主工作目录 `.emrg/`（项目会话/记忆副本）**不删除**——卸载报告列出位置
- 幂等：重复执行不报错，未找到项跳过
- **PATH 写入用锚点标记（R19）**：macOS/Linux 写 shell rc 时用**可识别锚点**（如 `# >>> emrg path >>>` / `# <<< emrg path <<<` 包裹的块），卸载时按锚点**精确删除该块**（不误删用户其他 PATH 配置）；GUI 用户（非开发者）不依赖 PATH（启动台/app 图标），PATH 只服务终端用户——用户级免 sudo 下 shell rc 是唯一可靠方式（/etc/paths.d 需 sudo ❌、launchctl 不继承终端 ⚠️）
- **Windows PATH（R27+R34）**：注册表 `HKCU\Environment\Path`（无锚点概念）——安装写**确定格式** `%USERPROFILE%\.emrg\install\bin`，卸载用 **Pascal 脚本读旧值 → 精确字符串替换移除 EMRG 段 → 写回**（`RegQueryStringValue`/`RegWriteStringValue`），不误删其他条目
- **平台卸载器的实现载体（R10+R15 核查）**：
  - **`emrg-uninstall` 脚本先执行内置 python 逻辑**（install/ 未删时 python 可用）——**⚠️ 卸载脚本无启动脚本的 PYTHONPATH，必须自设**（头部 `export PYTHONPATH=<prefix>/source:<prefix>/lib`，否则 `import emrg`/`websockets` 失败，R15 实证 connect.py:24 依赖 websockets）
  - 脚本做：停 daemon（**`from emrg.connect import connect_to_server` 发 shutdown 帧——R51 实证：emrg.connect 是独立模块无 CLI 副作用，卸载脚本可安全 import**；Windows 兜底 `taskkill`）→ 终止报告 → 墓地快照（tar 打包记忆/会话/演化日志）
  - 再删 install/（卸载器原生删除，此时 python 已退出无锁）
  - 最后清 PATH/快捷方式 + 自校验
  - macOS：卸载 app（shell 包装）调 `emrg-uninstall`；Windows：Inno Setup `[UninstallRun]` 调 `emrg-uninstall`（.cmd 包装）→ 原生删目录 + 快捷方式；Linux：终端跑 `emrg-uninstall` → 删 AppImage + 软链（R58）

---

## 7. 版本布局与原子切换（升级基础，v1 落地）

```
~/.emrg/
├── install/          # 当前版本（安装器放置 / 更新时原子替换）
├── versions/         # 历史版本（更新时保留，回滚用）——v1 由安装器/更新逻辑管理
└── install-info.json # git/gh 解析缓存（§5.2）
```

- 安装器（pkg/exe）直接写 `~/.emrg/install/`；AppImage 首次运行自解压
- **⚠️ R70（覆盖安装/升级须先停 daemon）**：`~/.emrg/install/` 正被运行中 daemon 占用——Windows 上 python.exe 文件锁导致复制失败，macOS 复制后 daemon 内存旧代码+文件新代码半新半旧。**安装器/升级流程第一步：停 daemon**（`emrg server stop` 或 shutdown 协议，复用 §6.2 卸载的同一停止逻辑；初次安装无 daemon 则跳过）——三平台一致（pkg postinstall / Inno Setup 安装段 / 新版本自解压前）
- **更新（方案 C 极简）**：新版本 = 停 daemon（R70）→ 替换 `source/` + lib/（构建期预装，R47）→ 重启 daemon。`emrg update` 二进制自更新为 **v1.1**（本次不做）——但 `_run_update` 需在打包模式下提示："打包版请从 GitHub Releases 下载新版本（v1.1 将支持 emrg update 自动更新）"。**打包模式判定**：`_find_source_dir()`（`__main__.py:359`）靠 `emrg.__file__` 上溯找 git repo——打包后 `__file__` 在 `source/emrg/` 内无 git repo → None → exit 前输出上述提示（替换现有生硬 `sys.exit(1)`）

---

## 8. CI 构建流水线（build-release.yml）

```yaml
# .github/workflows/build-release.yml
name: Build Release
on:
  push: { tags: ["v*"] }
  workflow_dispatch:

jobs:
  build:
    strategy:
      matrix:
        include:
          - os: macos-15        # arm64
          - os: ubuntu-24.04    # x86_64
          - os: ubuntu-24.04-arm  # aarch64（AppImage）
          - os: windows-2025    # x64
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { python-version: "3.13" }
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - run: uv sync --frozen
      - run: uv run pytest tests/ -v        # 回归门禁
      - run: uv python install 3.13         # standalone python → 缓存
      - run: bash packaging/build-runtime.sh ${{ matrix.os }}
        # → dist/runtime/：python 整目录 + source/ + lib/（构建期 pip --target 装好，R47）+ bin/emrgd 脚本
      - run: cd emrg/gui && npm ci && npm test   # GUI 单测
      - run: cd emrg/gui && npm run dist         # electron-builder（R22：只打包 GUI 本体，无 runtime 依赖）
      - run: bash packaging/bundle-git-gh.sh ${{ matrix.os }}   # git/gh → dist/runtime/bin/
        # ⚠️ R59：Windows 解压 Git for Windows portable → install/git/（整目录）；macOS/Linux 源码编译 git
        #   （无官方便携二进制，+3~5min）；gh 三平台官方单文件二进制
      - run: bash packaging/make-installer.sh ${{ matrix.os }}
        # 输入组装（R38）：dist/runtime/（bin+source+lib）+ GUI 产物（dist/mac-arm64/EMRG.app
        #   或 dist/win-unpacked/ 或 AppImage 本体）→ 平台 payload → dist/installers/EMRG-<v>.pkg 等
        # macOS pkgbuild / Windows Inno Setup / Linux AppImage + tar.gz 兜底
        # ⚠️ macOS 用户级 pkg（R54+R67）：pkgbuild install-location 不支持 ~ 展开——
        #   payload 装到临时位置 + postinstall 脚本复制到用户 ~/.emrg/install/
        #   ⚠️ R67：postinstall 的 $HOME 不可靠——GUI 安装器可能提权运行 postinstall（$HOME=/var/root）
        #   → postinstall 用 `stat -f "%Su" /dev/console` 获取控制台用户 + `dscl . -read /Users/<u> NFSHomeDirectory`
        #     查真实 HOME，取不到则 fallback $HOME
        # ⚠️ Windows 免 UAC（R55）：Inno Setup 配 PrivilegesRequired=lowest +
        #   DefaultDirName={userhome}\.emrg\install（{userhome} 常量，与 R34 统一）
        # ⚠️ 安装器第一步停 daemon（R70）：复制前 emrg server stop / shutdown 协议
        #   （覆盖安装/升级场景 install/ 被占用；初次安装无 daemon 跳过）
      - run: bash packaging/smoke-test.sh        # 产物冒烟（§9）
      - uses: softprops/action-gh-release@v2
        with: { files: "dist/installers/*" }
```

**要点**：
- **依赖顺序**：pytest → build-runtime（python+source+lib）→ npm dist（依赖 dist/runtime/）→ 捆绑 git/gh → 平台包装 → 冒烟
- **签名**：macOS 有证书则 `codesign + notarytool`（无证书产物标 `unsigned`，README 说明右键打开）；Windows EV 可选；Linux 无
- **冒烟在无 Python runner 上跑**（验证"干净机器"承诺）

---

## 9. 冒烟测试清单（构建产物，非源码）

**环境隔离（R42）**：`smoke-test.sh` 用**临时 HOME**（`HOME=$(mktemp -d)`，对齐 G73 集成测试隔离方案）跑全部用例——本地跑不污染真实 `~/.emrg`，CI 容器天然隔离。跑完清理临时 HOME。

**单测补充（R20+R24）**：`daemon_client.test.js` 新增打包分支用例——`new DaemonClient({ projectDir, isPackaged: true })` → `_findDaemonExecutable()` 返回 `~/.emrg/install/bin/emrgd`（R22 安装目录路径）→ 断言 startDaemon spawn 该路径（无 `-m` 参数）；现有用例不传 isPackaged → 源码模式（向后兼容已验证）。

| # | 用例 | 验证点 |
|---|------|--------|
| 1 | `emrg --version` | 启动脚本 + python + lib 链路 |
| 2 | `bin/emrgd` 拉起 daemon（→ port 文件 → auth_ok → pong） | 双入口 + PATH/PYTHONPATH（⚠️ R63：`emrg` 是 TUI，CI 无 TTY 会卡/报错——TUI 交互留本地手动；daemon 拉起用 §R43 实证的协议验证链路） |
| 3 | 聊天 + 工具调用 + 会话持久化 | 核心链路 |
| 4 | `emrg server stop` | daemon 生命周期 |
| 5 | `emrg rant "test"` | rant 链路（写 ~/.emrg/rants.jsonl） |
| 6 | 演化组件验证：`git --version` + `gh --version`（PATH 注入）+ 模板存在（`source/emrg/server/evolution_prompt.md`）+ `python -c "from emrg.skills.loader import ..."`（动态 import） | ⚠️ R68：**完整演化周期依赖 LLM + TUI /trigger（CLI 无 trigger 子命令），CI 无 TTY/无 key 跑不了**——拆为无 LLM 依赖的组件验证；完整周期留本地手动（同冒烟 3/8 降级模式） |
| 7 | **会话内 `python -c "print(1)"`** | §5.2 PATH 注入（捆绑 python 生效） |
| 8 | `emrg-gui` 启动 → 连接 daemon → 首启引导 | GUI 打包 + spawn ~/.emrg/install/bin/emrgd（R22）。**CI 无显示器（R39）**：Linux runner 无 X server——CI 冒烟 8 降级为 `EMRG.app/Contents/MacOS/EMRG --version` / `emrg-gui.exe --version` 验证入口存在（electron 支持 `--version` 不启窗）；**完整 GUI 冒烟（启窗+首启+聊天）留本地手动**（§1.3 范围已有） |
| 9 | GUI + TUI 同开同 session | 广播一致 |
| 10 | 平台卸载器（macOS 卸载 app / unins000.exe / Linux `emrg-uninstall`）→ 幂等重跑 → 自校验 | 卸载全流程（⚠️ R57：**不新增 `emrg uninstall` 命令**（§6 决策）——冒烟入口是平台卸载器） |
| 11 | 安装目录只读验证（`chmod -w` 后全功能跑） | 零写入审计 |
| 12 | **离线安装（无网络全程可用）** | R47：安装包自包含、零在线安装；安装 = 文件复制 |

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| standalone python 与 pyyaml wheel 架构不匹配 | pip 装失败 | 构建期在同平台安装（CI matrix 天然同架构，R11）；lib/ 预装打进安装包（R47） |
| 启动脚本相对定位失效（软链调用） | emrg 找不到 source | **R71 实测**：`BASH_SOURCE` 在软链调用时=软链路径（非真实路径）——必须 **readlink 循环解析**（§2.1 已含），软链场景（Linux `~/.local/bin/emrg`）实测通过 |
| 捆绑 git/gh 构建失败（某平台无便携版） | 平台安装不完整 | CI fail-fast：捆绑产物缺失即失败，不发残缺包 |
| Windows 杀软误报（python 脚本入口） | 安装被拦截 | 启动脚本 + 可选代码签名；文档说明 |
| 无 Apple 证书 Gatekeeper 拦截 | 首次运行需右键打开 | 产物标 unsigned + README 说明；有证书后启用公证 |
| 安装目录被误写 | daemon 崩溃/升级冲突 | 只读 + 冒烟 11 审计 |
| electron-builder 下载二进制（国内网络） | CI/本地构建慢 | CI 用 npm 镜像缓存；electron 二进制缓存（已实测 npmmirror 方案） |
| source/ 可读（非原生二进制） | 用户可改源码 | 开源项目无保密需求；只读权限 + 升级原子替换 |
| pyyaml 等含 C 扩展的 wheel 在 lib/ 的加载 | 找不到 .so | 构建期 pip --target 装好（R43 实测 PYTHONPATH 加载成功）+ 冒烟 3 |

---

## 11. 实施清单（一步交付，全部完成才合入）

**代码改造**：
- [ ] `bin/emrg`、`bin/emrgd` 启动脚本（§2.1，bash + Windows .cmd 双版；**含 R71 readlink 软链解析** + PYTHONDONTWRITEBYTECODE=1（R61）；Windows 版 PATH 含 `git\cmd`+`git\mingw64\bin`（R60）——Windows .cmd 走快捷方式(.lnk)无软链问题，`%~dp0` 正确）
- [ ] `bin/emrg-uninstall` 卸载脚本（§6.2 六步，三平台统一调用，R58）
- [ ] TUI 占位 api_key 提示（§3.4 R62：检测占位符 → 提示先运行 GUI 配置）
- [ ] `__main__.py:_run_update` 打包模式提示（v1.1 占位，替换生硬 sys.exit(1)）
- [ ] `git_utils.py`：`resolve_git_gh()` + `git_cmd()` + install-info.json（§5.2，兜底用）
- [ ] scheduler `_build_evolution_prompt` 注入 `{{ git_path }}`/`{{ gh_path }}`（§5.2）
- [ ] **`emrg/gui/daemon_client.js`：startDaemon 打包分支（§4.2，spawn 启动脚本）**

**打包资产**：
- [ ] `packaging/assets/` 图标（icns/ico/png）——**packaging/ 目录整体不存在，需新建**
- [ ] `packaging/build-runtime.sh`（python + source + lib 组装 → dist/runtime/）——**⚠️ python 复制细节（R16+R44+R45 实测）**：
  - **必须整目录复制** `cpython-3.13.9-<platform>-<arch>-none/` → `dist/runtime/bin/python-dist/`（含 `bin/python3.13` + **`lib/libpython3.13.dylib`**——R45 实测：只复制 bin/python3.13 会报 `Library not loaded: @executable_path/../lib/libpython3.13.dylib`，因二进制依赖 `../lib/` 的运行时库）
  - **⚠️ R69（目录名实测）**：uv 安装目录实际为 `cpython-<ver>-<platform>-<arch>-none/`（如 `cpython-3.13.9-macos-aarch64-none`）——build-runtime.sh **锁 patch 版本 `uv python install 3.13.9` + 动态 glob** `cpython-3.13.9-*/`（勿硬编码平台段，uv 命名随版本演进）
  - **重建软链**（R44 实测：uv 的 `bin/python`/`bin/python3` 软链指向 `$HOME/.local/share/uv/...` 绝对路径，复制后失效）：
    `dist/runtime/bin/python` → `python-dist/bin/python3.13`（相对软链）
  - standalone 复制后**相对自身定位**（sys.prefix=复制位置，R44c 实测不依赖 HOME）——冒烟隔离（临时 HOME）不破坏
- [ ] `packaging/bundle-git-gh.sh`（git/gh 捆绑 → dist/runtime/bin/）
- [ ] `packaging/make-installer.sh`（pkgbuild / Inno Setup / AppImage + tarball 兜底；**第一步停 daemon（R70）**；**含平台卸载器 §6：终止报告 + 墓地快照 + 清理 + 自校验**）
- [ ] `packaging/smoke-test.sh`（§9 清单 12 项）
- [ ] `packaging/requirements.lock`（依赖版本锁定，R47：构建期 pip --target 预装，无需 wheels 目录）

**CI**：
- [ ] `.github/workflows/build-release.yml`（§8，tag v* 触发）

**文档**：
- [ ] README 安装/卸载章节重写（分平台"下载 → 双击安装"指引）
- [ ] roadmap.md Phase 4 验收勾选

**验收**：§1.2 全部 8 项在干净容器通过。
