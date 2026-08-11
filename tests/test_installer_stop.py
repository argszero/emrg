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
    # step 4（rant 2026-08-11T17:56:25 → 18:56:58 修正）：只杀 EMRG 自己 git 子进程树，
    # 不碰宿主 Git Bash sh/vim（R125 同族——宿主工具不干涉）。
    # 判别：ExecutablePath 前缀过滤只命中 %INSTALL%\git\（不误杀系统 Git）。
    # ⚡ review 2026-08-11T19:03：祖先回溯（ancestor walk）无法解析"已死父进程"——
    # #683 主场景正是 daemon 已死后的孤儿 → 改为 step 0 **向下**快照
    # （daemon/TUI/GUI 根 + BFS 子孙集，写入 %TEMP%\emrg-stop-pids.txt），
    # step 4 / :verify 只作用于快照记录集。
    assert 'Get-CimInstance Win32_Process' in content
    assert r'$env:USERPROFILE\.emrg\install\git\*' in content
    # step 0 快照：向下 BFS（ParentProcessId → 子进程加入集合）在杀任何进程之前
    assert "emrg-stop-pids.txt" in content
    assert "ParentProcessId" in content
    assert "-match '-m emrg'" in content
    assert "Set-Content" in content  # 快照写盘
    # step 4 的 kill 必须基于快照记录集（$ids -contains）+ ExecutablePath 过滤
    step4_idx = content.index("REM --- 4. bundled git: kill only RECORDED")
    daemon_line2 = content.index('call "%INSTALL%\\bin\\emrg.cmd" server stop')
    verify_idx = content.index(':verify\nset "EXIT_CODE=0"')  # 标签定义处（goto :verify 在前，勿用裸 index(":verify")）
    assert daemon_line2 < step4_idx < verify_idx
    assert "$ids -contains [int]$_.ProcessId" in content
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
    # :verify 的 EMRG-owned bundled-git 存活判定：只有快照记录集内的 EMRG git PID 残留
    # → exit 1；宿主 sh/vim 存活不是失败（rant 2026-08-11T18:56:58）——判别：verify 块
    # 含 $ids -contains（快照集过滤），但不再有无差别 ExecutablePath 存活检查
    assert "exit 1" in verify_block
    assert "if errorlevel 1 set \"EXIT_CODE=1\"" in verify_block
    assert "$ids -contains" in verify_block
    # 无差别 bundled-git 存活检查必须已删除：verify 块里不得再有"任何 install\git\ 进程
    # 即 exit 1"的旧逻辑（旧式：if (Get-CimInstance ...) { exit 1 }）
    assert "}) { exit 1 }" not in verify_block
    # 而 step 4 的 kill 命令必须保留（快照集过滤 + ExecutablePath）
    assert "Stop-Process" in content
    # 快照文件清理
    assert 'del /q "%PIDFILE%"' in content


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
