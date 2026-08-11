"""Windows 安装器预停止接线回归测试（rant 2026-08-10T08:50:44）。

安装器（Inno Setup，make-installer.sh 生成 emrg.iss）覆盖 ~/.emrg/install 前必须
先优雅关闭 GUI/TUI/daemon，否则无窗口的 pythonw daemon 独占锁文件 → 卡在
"停止已有进程"（宿主只能重启系统）。本测试纯文本断言（不执行 iscc/cmd ——
macOS/CI 无 Windows），钉死四处接线：
  1. bin/stop-emrg.cmd 存在且覆盖三步：GUI 优雅关闭+/F 兜底、TUI 命令行过滤、
     daemon 协议关闭 + emrgd.pid 轮询兜底（顺序 GUI → daemon）
  2. bin/emrgd.cmd 含 stop 分支（复用 `emrg server stop`）
  3. make-installer.sh 的 .iss 模板含 [Files] dontcopy + [Code] PrepareToInstall
  4. build-runtime.sh 把 stop-emrg.cmd 复制进 runtime bin/
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_stop_emrg_cmd_covers_gui_tui_daemon_in_order():
    content = (REPO_ROOT / "bin" / "stop-emrg.cmd").read_text(encoding="utf-8")
    # GUI：无 /F 优雅 WM_CLOSE 优先，/F 兜底
    assert "taskkill /IM EMRG.exe" in content
    assert "taskkill /F /IM EMRG.exe" in content
    assert content.index("taskkill /IM EMRG.exe") < content.index("taskkill /F /IM EMRG.exe")
    # 宿主 2026-08-10T01:27:07Z 实测：长活 ~15h GUI 会话 WM_CLOSE 在 5s 窗内未退出，
    # 两次整跑未终止、直接 taskkill /F 才终止 → /F 必须是无条件兜底（不 gate 在
    # survivor 检查上），否则旧 GUI 会话可无限期拖住安装器。判别：ping 等待之后、
    # /F 之前不得再有 findstr 判定。
    ping_idx = content.index("ping -n 6 127.0.0.1")
    f_idx = content.index("taskkill /F /IM EMRG.exe")
    assert ping_idx < f_idx
    assert "findstr" not in content[ping_idx:f_idx]
    # TUI：命令行过滤（wmic LIKE 通配符须 %% 转义）
    assert "wmic" in content and "commandline like" in content
    assert "powershell" in content  # wmic 缺失（Win11 24H2+）时的回退
    # daemon：协议关闭 + pid 轮询兜底（用实际执行行而非注释里的字面量）
    assert 'call "%INSTALL%\\bin\\emrg.cmd" server stop' in content
    assert "emrgd.pid" in content
    # 顺序：GUI 在 daemon 之前（GUI 不能复活 daemon）
    daemon_line = content.index('call "%INSTALL%\\bin\\emrg.cmd" server stop')
    assert content.index("taskkill /IM EMRG.exe") < daemon_line
    # step 4（rant 2026-08-11T17:56:25 → 18:56:58 修正）：只杀 EMRG 自己 git 子进程树
    # （祖先链含 daemon/TUI），不碰宿主 Git Bash sh/vim（R125 同族——宿主工具不干涉）。
    # 判别：ExecutablePath 前缀过滤只命中 %INSTALL%\git\（不误杀系统 Git），
    # 且用 ParentProcessId 祖先回溯（≤5 层）+ `-m emrg` 判定 EMRG 归属；
    # daemon 停止之后、:verify 之前。
    assert 'Get-CimInstance Win32_Process' in content
    assert r'$env:USERPROFILE\.emrg\install\git\*' in content
    # 祖先回溯是 step 4 的核心判别（宿主 Git Bash sh/vim 的祖先链是 explorer/终端 → 不杀）
    assert "ParentProcessId" in content
    assert "-match '-m emrg'" in content
    # 锚定实际 kill 命令（$_.ExecutablePath 只在 step 4 出现——TUI 分支是
    # -Filter Name='python.exe'，不共享此模式），勿用 index() 撞到 TUI/REM 注释
    git_kill = content.index("$_.ExecutablePath -like")
    daemon_line2 = content.index('call "%INSTALL%\\bin\\emrg.cmd" server stop')
    verify_idx = content.index(':verify\nset "EXIT_CODE=0"')  # 标签定义处（goto :verify 在前，勿用裸 index(":verify")）
    assert daemon_line2 < git_kill < verify_idx
    # 干净安装安全：无旧 install 目录时跳过
    assert 'set "INSTALL=%EMRG_DIR%\\install"' in content
    # 括号块内 pid 判定必须用延迟展开（!DPID!）——%DPID% 在块解析时展开，
    # set "DPID=" 后取到旧值/空值 → if defined 恒假 → daemon 存活误报干净
    # （rant 2026-08-10T08:50:44，cmd.exe 经典括号块展开坑）
    assert "setlocal enabledelayedexpansion" in content
    # 从标签定义处（而非前面 wait 循环的 goto :verify）截取校验块
    verify_block = content[content.index(":verify\nset \"EXIT_CODE=0\""):]
    assert "PID eq !DPID!" in verify_block
    # 非延迟展开 %DPID% 不得出现在括号块内（块解析时展开=恒旧值）
    assert "%DPID%" not in verify_block
    # :verify 的 EMRG-owned bundled-git 存活判定：只有 EMRG git 子树残留 → exit 1；
    # 宿主 sh/vim 存活不是失败（rant 2026-08-11T18:56:58）——判别：verify 块含
    # 祖先回溯判定（ParentProcessId + -m emrg），但不再有无差别 ExecutablePath 存活检查
    assert "exit 1" in verify_block
    assert "if errorlevel 1 set \"EXIT_CODE=1\"" in verify_block
    assert "ParentProcessId" in verify_block
    assert "-match '-m emrg'" in verify_block
    # 无差别 bundled-git 存活检查必须已删除：verify 块里不得再有"任何 install\git\ 进程
    # 即 exit 1"的旧逻辑（旧式：if (Get-CimInstance ...) { exit 1 }）
    assert "}) { exit 1 }" not in verify_block
    # 而 step 4 的 kill 命令必须保留（EMRG-owned 判定在 kill 与 verify 都出现）
    assert "Stop-Process" in content


def test_emrgd_cmd_has_stop_branch():
    content = (REPO_ROOT / "bin" / "emrgd.cmd").read_text(encoding="utf-8")
    assert 'if /I not "%~1"=="stop" goto :start' in content
    assert "-m emrg server stop" in content
    assert "exit /b %errorlevel%" in content


def test_make_installer_iss_has_prepare_to_install():
    content = (REPO_ROOT / "packaging" / "make-installer.sh").read_text(encoding="utf-8")
    assert "dontcopy" in content
    assert "stop-emrg.cmd" in content
    assert "PrepareToInstall" in content
    assert "ExtractTemporaryFile('stop-emrg.cmd')" in content
    assert "SW_HIDE" in content  # 批处理执行不弹控制台窗口（#592 纪律）
    # rant 2026-08-11T17:56:25：中止消息含重启兜底引导（杀不掉时宿主可重启后重试）
    assert "restart the computer" in content


def test_build_runtime_copies_stop_emrg_cmd():
    content = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/bin/stop-emrg.cmd" stop-emrg.cmd' in content
