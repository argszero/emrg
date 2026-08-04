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
| Windows | `EMRG-Setup-<ver>.exe` | 双击 → 安装向导 → 完成；开始菜单出现 `EMRG` 快捷方式；终端可用 `emrg` | `%LOCALAPPDATA%\EMRG\` + PATH |
| Linux | `EMRG-<ver>-linux-<arch>.AppImage` | 下载 → chmod +x → 双击运行（GUI）；**首次运行自解压到 `~/.emrg/install/` + 建 `~/.local/bin/emrg` 启动器** | 单文件 + `~/.local/bin` |

### 1.2 验收标准（全部一次满足）

- [ ] 干净容器（无 python/uv/git/gh/node）→ 双击安装 → 无报错
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
| 体积 | ~240MB（**3 套 python**：emrg onedir + emrgd onedir + 会话用 standalone） | ~120MB（**1 套**） |
| 会话内跑 python 脚本 | ❌ 需另捆绑 standalone | ✅ **同一套 python 天然支持** |
| 模板收集 | ❌ spec datas 手动精确路径（prompt.md 在 server/ 根） | ✅ 源码原样 |
| frozen 分支 | ❌ 需改 `__main__.py` + GUI resourcesPath | ✅ 启动脚本即可 |
| 依赖重复（emrg/emrgd） | ❌ 各一份解释器+依赖 | ✅ 共享 |
| 更新 | 重新 PyInstaller 构建 | 替换 source/ + pip install |
| 启动速度 | 快（C bootloader） | 稍慢 ~300ms（可接受） |

**核心洞察**：PyInstaller 解决的"没 python 也能跑"——但我们**反正要捆绑 python 给会话内脚本用**（§5.0 已论证），那就直接用这一个 python 跑源码，PyInstaller 变成纯冗余。standalone python（uv 官方，含 pip）52MB、依赖 site-packages 20MB、源码 ~1MB，一套搞定。

---

## 2. 安装产物结构（三平台统一）

```
<prefix>/                         # macOS: ~/.emrg/install/；Windows: %LOCALAPPDATA%\EMRG\；Linux: AppImage 内
├── bin/
│   ├── python                   # ⭐ standalone CPython（uv 官方，含 pip，52MB）——唯一一套 python
│   ├── python3                  # 符号链接
│   ├── emrg                     # 启动脚本：exec python -m emrg（R13：-m 包入口 + PYTHONPATH=source:lib）
│   ├── emrgd                    # 启动脚本：exec python -m emrg.server（同 R13）
│   ├── git                      # 捆绑 git（平台便携版）
│   ├── gh                       # 捆绑 gh CLI（官方单文件二进制）
│   └── emrg-gui/                # Electron 产物（emrg-gui.app / emrg-gui.exe / emrg-gui）
├── lib/                         # 依赖 site-packages（20MB，安装器预装：python -m pip install）
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
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$(dirname "$DIR")"
export PATH="$DIR:$PATH"                    # python/git/gh 对子进程可见
export PYTHONPATH="$PREFIX/source:$PREFIX/lib:$PYTHONPATH"   # ⚠️ R13：必须含 source/（emrg 包父目录）
exec "$DIR/python" -m emrg "$@"             # ⚠️ R13：用 -m emrg（包入口），非 python source/emrg/__main__.py
```

```bash
#!/usr/bin/env bash
# bin/emrgd — daemon 入口（独立进程，长期运行）
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="$(dirname "$DIR")"
export PATH="$DIR:$PATH"
export PYTHONPATH="$PREFIX/source:$PREFIX/lib:$PYTHONPATH"
exec "$DIR/python" -m emrg.server "$@"
```

- **⚠️ R13（关键，实测验证）**：`python source/emrg/__main__.py` 直接跑会 `ModuleNotFoundError: No module named 'emrg'`——因为 sys.path[0] = `source/emrg/`（脚本所在目录），而 `import emrg` 需要 `source/`（包父目录）在 path。**必须**：
  1. `PYTHONPATH` 含 `$PREFIX/source`（emrg 包父目录）
  2. 用 `-m emrg` / `-m emrg.server`（包入口，`__main__` 由包机制解析）——实测 `emrg 0.2.0` 正常输出
- **Windows 版**：`.cmd` 批处理同逻辑（`%~dp0` 定位；`set PATH=%~dp0;%PATH%`、`set PYTHONPATH=<prefix>\source;<prefix>\lib;%PYTHONPATH%`、`python -m emrg %*`）
- **PATH 导出**：启动脚本内 `export PATH="$DIR:$PATH"` 使 daemon 继承后，bash 工具子进程（`create_subprocess_shell`）能直接找到 `python`/`git`/`gh`——**这是会话内脚本能力的关键**（§5.0）

### 2.2 ⚠️ source/ 复制必须排除 emrg/gui（R14，448MB 陷阱）

`emrg/gui/` 是 emrg 包的子目录，但**含 node_modules 448MB**（`du -sh emrg/gui` 实测）；Python 运行时**零依赖它**（grep 实证：无 `import emrg.gui`）。

**build-runtime.sh 的 source/ 组装**（精确复制，勿整包拷贝）：
```
复制（必需）：
  emrg/__init__.py  emrg/__main__.py  emrg/config.py  emrg/connect.py  emrg/protocol.py  emrg/session.py  emrg/memory.py
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

### 3.2 安装器预装逻辑

安装器（或首次运行脚本）执行：
```
"$PREFIX/bin/python" -m pip install --target "$PREFIX/lib" \
    rich httpx pyyaml jinja2 websockets
```
- **⚠️ 必须全量装（含传递依赖），禁用 `--no-deps`**（R3 实测：httpx→httpcore/h11/certifi、rich→markdown-it-py/pygments、jinja2→markupsafe 都是必需的——`--no-deps` 后 `import httpx` 直接失败）
- `--target lib/`：装到安装目录（只读）
- **PYTHONPATH 注入**：emrg 启动脚本 `export PYTHONPATH="$PREFIX/source:$PREFIX/lib"`（与 PATH 并列，R13）——daemon 与 TUI 都从 lib/ 加载依赖、从 source/ 加载 emrg 包
- **pip 本身**：standalone python 自带（无需额外捆绑）
- **C 扩展**：pyyaml 的 `_yaml.so` 经 PYTHONPATH 正常加载（R1/R2 实测通过）

### 3.3 依赖收集（CI 构建期）

CI 用 `uv pip compile` 或直接 `pip download` 锁定版本 → 生成 `packaging/requirements.lock`（**含全部传递依赖**）→ 安装器据此下载/预装。**离线安装**：构建期把 wheels（含传递依赖）打进安装包 `wheels/` 目录，安装器 `pip install --no-index --find-links`（干净机器无网也可装——与"双击安装即完整"一致）。

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
    // R22：固定走安装目录（三平台一致，与 TUI 共享同一 daemon 版本）
    return path.join(os.homedir(), ".emrg", "install", "bin", "emrgd");
  }
  return null;  // 源码模式走 _findPython()
}
// startDaemon()：打包模式 spawn 安装目录 emrgd（无参数，脚本内部 exec python + 设 PATH/PYTHONPATH）
//               源码模式保持现状（.venv python -m emrg.server）
```
- 启动脚本自行定位 python/source/lib，GUI 只需 spawn 它
- cwd=projectDir（G125）/ stdio ignore（G68）/ detached / unref 选项不变
- **两套 runtime 不冲突（R22 实证）**：daemon pid 文件互斥（daemon.py:130-141）+ 共享 port 文件——先到先得，第二个自我退出；GUI 与 TUI 始终直连同一 daemon
- **AppImage 时序（R25）**：AppImage 是 GUI 容器——**自解压逻辑归 AppImage 启动器**（首次运行先解压 runtime 到 `~/.emrg/install/` 再启动 GUI）；GUI 的 `_findDaemonExecutable()` 发现 `~/.emrg/install/bin/emrgd` 不存在时 → **提示"请先运行 AppImage 完成安装"或触发自解压回调**（v1 简单处理：提示 + 退出；确保 pkg/exe 安装场景 install/ 恒存在）
- **electron-builder 配置变化（R23 实证）**：`emrg/gui/package.json` build 段**删除 `extraResources`**（现为 `[{from: '../dist/emrgd', to: 'emrgd'}]`——Phase 3 PyInstaller 遗留，R22 后 GUI 不再携带 runtime）：
```json
"build": {
  "appId": "com.emrg.gui",
  "productName": "EMRG",
  "target": ["dmg", "exe", "AppImage"],
  "icon": "../packaging/assets/",
  "mac": { "identity": null }
}
```
- cwd=projectDir（G125）/ stdio ignore（G68）/ detached / unref 选项不变

### 4.3 构建顺序

```
构建 runtime（python + source + lib + emrgd 脚本）→ dist/runtime/（安装器用，非 GUI 内嵌）
cd emrg/gui && npm run dist   # electron-builder 只打包 GUI 本体（无 extraResources runtime）
平台包装：dist/runtime/ + dist/gui/ 组装进安装器（pkg/exe/AppImage）
```

---

## 5. git/gh 捆绑 + PATH 注入

### 5.1 捆绑来源（CI 下载，随包分发）

| 平台 | git | gh |
|------|-----|-----|
| macOS | 便携 git 构建（CI 下载官方 macOS git 二进制） | gh 官方 release 单文件二进制 |
| Windows | Git for Windows portable（官方单文件自解压 → 解压入 bin/） | 同上 |
| Linux | 静态/便携 git 构建 | 同上 |

随包附各自 LICENSE（git: GPL-2.0 独立可执行；gh: MIT）。

### 5.2 PATH 注入（主路径，覆盖三件套）

**启动脚本已 `export PATH="$DIR:$PATH"`（§2.1）**——`python`/`git`/`gh` 对 daemon 及 bash 工具子进程全部可见：

- **会话内 `python script.py`** ✅（§1.2 验收项——EMRG 与用户脚本共享同一 python）
- **git**：`["git", ...]` 裸调用自动命中捆绑 git（scheduler.py:164 / git_utils.py:15 / __main__.py:325 无需改——PATH 优先）
- **gh**：`which gh` 命中捆绑 gh

**⚠️ AppImage 特例（R8 实测分析）**：
- AppImage 运行 = 挂载到 `/tmp/.mount_XXXX`（**每次随机**）→ 启动脚本的 PATH 注入指向**临时挂载目录**——运行期间可用，但：
  - `~/.local/bin/emrg` **不能是符号链接**（挂载路径每次变）→ 必须是**启动器脚本**：`exec <AppImage绝对路径>`（首次运行时把 AppImage 绝对路径写入启动器）
  - **长期进程（daemon 由 GUI 拉起）** 的 PATH 注入在 AppImage 退出后失效 → **首次运行必须把 python/git/gh 复制到 `~/.emrg/install/bin/`**（数据目录可写），启动器 exec AppImage 前先确保复制完成
- 因此 AppImage 的**首次运行自解压**是必须的：`bin/python`/`bin/git`/`bin/gh` → `~/.emrg/install/bin/`，source/lib → `~/.emrg/install/`（与 pkg/exe 安装后的布局一致）——**AppImage 实际是"自解压安装器"**，之后 PATH 注入走 `~/.emrg/install/bin/`（稳定路径，非临时挂载）

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
| macOS | pkg 卸载器（双击，pkgbuild 生成） | 停 daemon → 终止报告 + 墓地快照 → 删 install/ → 删 ~/.emrg 数据 → 清 shell rc 的 PATH |
| Windows | 控制面板卸载（Inno Setup 生成 unins000.exe） | 停 daemon → 终止报告 + 墓地快照 → 删 install/ → 删 %LOCALAPPDATA%\EMRG\ 数据 → 清注册表 PATH + 快捷方式 |
| Linux | 删除 AppImage 文件（+ 可选 `rm -rf ~/.emrg`） | AppImage 单文件即删即走；tarball 版删解压目录 |

### 6.2 终止报告 + 墓地快照（对齐 MANIFESTO 第十条【终止权】）

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
- **平台卸载器的实现载体（R10+R15 核查）**：
  - 卸载器先执行**内置 python 脚本**（install/ 未删时 python 可用）——**⚠️ 卸载脚本无启动脚本的 PYTHONPATH，必须自设**（头部 `export PYTHONPATH=<prefix>/source:<prefix>/lib`，否则 `import emrg`/`websockets` 失败，R15 实证 connect.py:24 依赖 websockets）
  - 脚本做：停 daemon（走 shutdown 协议，`import emrg.connect` 或读 port 文件发 ws 消息；Windows 兜底 `taskkill`）→ 终止报告 → 墓地快照（tar 打包记忆/会话/演化日志）
  - 再删 install/（卸载器原生删除，此时 python 已退出无锁）
  - 最后清 PATH/快捷方式 + 自校验
  - macOS：pkg 卸载器（postinstall 反向脚本）；Windows：Inno Setup 卸载段（`[UninstallRun]` 跑 python 脚本 + 原生删目录）；Linux：删 AppImage 文件即卸载（tarball 删解压目录）

---

## 7. 版本布局与原子切换（升级基础，v1 落地）

```
~/.emrg/
├── install/          # 当前版本（安装器放置 / 更新时原子替换）
├── versions/         # 历史版本（更新时保留，回滚用）——v1 由安装器/更新逻辑管理
└── install-info.json # git/gh 解析缓存（§5.2）
```

- 安装器（pkg/exe）直接写 `~/.emrg/install/`；AppImage 首次运行自解压
- **更新（方案 C 极简）**：新版本 = 替换 `source/` + `pip install` 增量 + 重启 daemon。`emrg update` 二进制自更新为 **v1.1**（本次不做）——但 `_run_update` 需在打包模式下提示："打包版请从 GitHub Releases 下载新版本（v1.1 将支持 emrg update 自动更新）"。**打包模式判定**：`_find_source_dir()`（`__main__.py:359`）靠 `emrg.__file__` 上溯找 git repo——打包后 `__file__` 在 `source/emrg/` 内无 git repo → None → exit 前输出上述提示（替换现有生硬 `sys.exit(1)`）

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
        # → dist/runtime/：python 软链 + source/ + lib/（pip download wheels → 安装器 --no-index）+ bin/emrgd 脚本
      - run: cd emrg/gui && npm ci && npm test   # GUI 单测
      - run: cd emrg/gui && npm run dist         # electron-builder（R22：只打包 GUI 本体，无 runtime 依赖）
      - run: bash packaging/bundle-git-gh.sh ${{ matrix.os }}   # git/gh → dist/runtime/bin/
      - run: bash packaging/make-installer.sh ${{ matrix.os }}
        # macOS pkgbuild / Windows Inno Setup / Linux AppImage + tar.gz 兜底
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

**单测补充（R20+R24）**：`daemon_client.test.js` 新增打包分支用例——`new DaemonClient({ projectDir, isPackaged: true })` → `_findDaemonExecutable()` 返回 `~/.emrg/install/bin/emrgd`（R22 安装目录路径）→ 断言 startDaemon spawn 该路径（无 `-m` 参数）；现有用例不传 isPackaged → 源码模式（向后兼容已验证）。

| # | 用例 | 验证点 |
|---|------|--------|
| 1 | `emrg --version` | 启动脚本 + python + lib 链路 |
| 2 | `emrg` 启动 → daemon 拉起（bin/emrgd 脚本） | 双入口 + PATH/PYTHONPATH |
| 3 | 聊天 + 工具调用 + 会话持久化 | 核心链路 |
| 4 | `emrg server stop` | daemon 生命周期 |
| 5 | `emrg rant "test"` | rant 链路（写 ~/.emrg/rants.jsonl） |
| 6 | 演化干跑（trigger evolution） | 模板源码 + skills 动态 import + git/gh |
| 7 | **会话内 `python -c "print(1)"`** | §5.2 PATH 注入（捆绑 python 生效） |
| 8 | `emrg-gui` 启动 → 连接 daemon → 首启引导 | GUI 打包 + spawn ~/.emrg/install/bin/emrgd（R22） |
| 9 | GUI + TUI 同开同 session | 广播一致 |
| 10 | `emrg uninstall` → 幂等重跑 → 自校验 | 卸载全流程 |
| 11 | 安装目录只读验证（`chmod -w` 后全功能跑） | 零写入审计 |
| 12 | 离线安装（无网络，wheels/ 预装） | 3.3 离线能力 |

---

## 10. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| standalone python 与 pyyaml wheel 架构不匹配 | pip 装失败 | 构建期在同平台下载 wheel（CI matrix 天然同架构）；`--no-index` 离线装 |
| 启动脚本相对定位失效（软链调用） | emrg 找不到 source | 用 `BASH_SOURCE`/`%~dp0` 解析真实路径（非 `$0`）；软链场景实测 |
| 捆绑 git/gh 构建失败（某平台无便携版） | 平台安装不完整 | CI fail-fast：捆绑产物缺失即失败，不发残缺包 |
| Windows 杀软误报（python 脚本入口） | 安装被拦截 | 启动脚本 + 可选代码签名；文档说明 |
| 无 Apple 证书 Gatekeeper 拦截 | 首次运行需右键打开 | 产物标 unsigned + README 说明；有证书后启用公证 |
| 安装目录被误写 | daemon 崩溃/升级冲突 | 只读 + 冒烟 11 审计 |
| electron-builder 下载二进制（国内网络） | CI/本地构建慢 | CI 用 npm 镜像缓存；electron 二进制缓存（已实测 npmmirror 方案） |
| source/ 可读（非原生二进制） | 用户可改源码 | 开源项目无保密需求；只读权限 + 升级原子替换 |
| pyyaml 等含 C 扩展的 wheel 在 lib/ 的加载 | 找不到 .so | 安装器 `pip install --target lib/` 正确放置 + PYTHONPATH 注入 + 冒烟 3 |

---

## 11. 实施清单（一步交付，全部完成才合入）

**代码改造**：
- [ ] `bin/emrg`、`bin/emrgd` 启动脚本（§2.1，bash + Windows .cmd 双版）
- [ ] `__main__.py:_run_update` 打包模式提示（v1.1 占位，替换生硬 sys.exit(1)）
- [ ] `git_utils.py`：`resolve_git_gh()` + `git_cmd()` + install-info.json（§5.2，兜底用）
- [ ] scheduler `_build_evolution_prompt` 注入 `{{ git_path }}`/`{{ gh_path }}`（§5.2）
- [ ] **`emrg/gui/daemon_client.js`：startDaemon 打包分支（§4.2，spawn 启动脚本）**

**打包资产**：
- [ ] `packaging/assets/` 图标（icns/ico/png）——**packaging/ 目录整体不存在，需新建**
- [ ] `packaging/build-runtime.sh`（python + source + lib 组装 → dist/runtime/）——**⚠️ python 复制必须保留软链**（R16：standalone 的 `bin/python`/`bin/python3` 是软链指向 `python3.13`；用 tar/rsync -a 保留，勿用跟随软链的 cp）
- [ ] `packaging/bundle-git-gh.sh`（git/gh 捆绑 → dist/runtime/bin/）
- [ ] `packaging/make-installer.sh`（pkgbuild / Inno Setup / AppImage + tarball 兜底；**含平台卸载器 §6：终止报告 + 墓地快照 + 清理 + 自校验**）
- [ ] `packaging/smoke-test.sh`（§9 清单 12 项）
- [ ] `packaging/requirements.lock`（依赖版本锁定 + wheels 目录）

**CI**：
- [ ] `.github/workflows/build-release.yml`（§8，tag v* 触发）

**文档**：
- [ ] README 安装/卸载章节重写（分平台"下载 → 双击安装"指引）
- [ ] roadmap.md Phase 4 验收勾选

**验收**：§1.2 全部 8 项在干净容器通过。
