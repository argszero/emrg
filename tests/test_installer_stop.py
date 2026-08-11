"""Windows 安装器预停止接线回归测试（rant 2026-08-10T08:50:44 + 宿主 2026-08-11T19:47:44 连坐强杀拍板）。

安装器（Inno Setup，make-installer.sh 生成 emrg.iss）覆盖 ~/.emrg/install 前必须
先优雅关闭 GUI/TUI/daemon/bundled-git，否则无窗口的 pythonw daemon 独占锁文件 → 卡在
"停止已有进程"（宿主只能重启系统）。本测试纯文本断言（不执行 iscc/cmd ——
macOS/CI 无 Windows），钉死接线：
  1. bin/stop-emrg.cmd 覆盖三步 + step 4 调 stop-git.ps1：GUI 优雅关闭+/F 兜底、
     TUI 命令行过滤、daemon 协议关闭 + emrgd.pid 轮询兜底（顺序 GUI → daemon）
  2. bin/stop-git.ps1 独立脚本：install\\git\\ 前缀收集 → 先杀 git 树 → 连坐强杀全部
     残留（sh/vim 一并杀，宿主拍板覆盖 #689）→ 无残留 exit 0 / 有残留 exit 1
  3. bin/emrgd.cmd 含 stop 分支（复用 `emrg server stop`）
  4. make-installer.sh 的 .iss 模板含 [Files] dontcopy（stop-emrg.cmd + stop-git.ps1）
     + [Code] PrepareToInstall 传 {tmp} 提取版作 %~1
  5. build-runtime.sh 把 stop-emrg.cmd / stop-git.ps1 复制进 runtime bin/
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_stop_emrg_cmd_covers_gui_tui_daemon_and_calls_stop_git():
    content = (REPO_ROOT / "bin" / "stop-emrg.cmd").read_text(encoding="utf-8")
    # GUI：无 /F 优雅 WM_CLOSE 优先，/F 兜底
    assert "taskkill /IM EMRG.exe" in content
    assert "taskkill /F /IM EMRG.exe" in content
    assert content.index("taskkill /IM EMRG.exe") < content.index("taskkill /F /IM EMRG.exe")
    # 宿主 2026-08-10T01:27:07Z 实测：长活 ~15h GUI 会话 WM_CLOSE 在 5s 窗内未退出，
    # 两次整跑未终止、直接 taskkill /F 才终止 → /F 必须是无条件兜底（不 gate 在
    # survivor 检查上）。判别：ping 等待之后、/F 之前不得再有 findstr 判定。
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
    # step 4（宿主 2026-08-11T19:47:44 拍板覆盖 #689）：调独立 stop-git.ps1 连坐强杀。
    # 判别：powershell -File 调用（无 cmd 内联转义）+ -ExecutionPolicy Bypass。
    step4_idx = content.index("REM --- 4. bundled git: guilt-by-association")
    verify_idx = content.index("\n:verify\n")  # 标签定义处（\n 前缀排除前面 goto :verify 行）
    assert daemon_line < step4_idx < verify_idx
    assert "stop-git.ps1" in content
    assert "-ExecutionPolicy Bypass -File" in content
    assert r'"%GITSTOP%"' in content
    # 旧安装可能没有 stop-git.ps1 → {tmp} 提取版（%~1）兜底
    assert 'set "GITSTOP=%~1"' in content
    # %TEMP% 兜底已移除（review 2026-08-11T19:57: {tmp} 参数或已装副本恒胜出；缺失时 :verify 前缀检查兜底）
    assert "GITSTOP=%TEMP%" not in content
    # #689 的 snapshot/step0/PIDFILE 机制已删除（宿主否决——误伤宿主工具的根源）
    assert "emrg-stop-pids.txt" not in content
    assert "ParentProcessId" not in content
    assert "PIDFILE" not in content
    assert "Set-Content" not in content  # 快照写盘命令不复存在
    # step 4 失败必须传导到退出码（EXIT_CODE 初始化在 setlocal 之后、verify 不再清零）
    assert 'set "EXIT_CODE=0"' in content
    assert "if errorlevel 1 set \"EXIT_CODE=1\"" in content
    assert content.index('set "EXIT_CODE=0"') < step4_idx
    # 干净安装安全：无旧 install 目录时跳过
    assert 'set "INSTALL=%EMRG_DIR%\\install"' in content
    # 括号块内 pid 判定必须用延迟展开（!DPID!）——%DPID% 在块解析时展开，
    # set "DPID=" 后取到旧值/空值 → if defined 恒假 → daemon 存活误报干净
    # （rant 2026-08-10T08:50:44，cmd.exe 经典括号块展开坑）
    assert "setlocal enabledelayedexpansion" in content
    # 从标签定义处截取校验块
    verify_block = content[content.index("\n:verify\n"):]
    assert "PID eq !DPID!" in verify_block
    # 非延迟展开 %DPID% 不得出现在括号块内（块解析时展开=恒旧值）
    assert "%DPID%" not in verify_block
    # :verify 的 bundled-git 存活判定：无差别前缀检查（连坐语义——连坐杀后仍存活才中止）
    assert "exit 1" in verify_block
    assert "if errorlevel 1 set \"EXIT_CODE=1\"" in verify_block
    assert r'$env:USERPROFILE\.emrg\install\git\*' in verify_block
    assert "Get-CimInstance" in verify_block


def test_stop_git_ps1_guilt_by_association():
    content = (REPO_ROOT / "bin" / "stop-git.ps1").read_text(encoding="utf-8")
    # 前缀过滤：只碰 %USERPROFILE%\.emrg\install\git\*（系统 Git 永不命中）
    assert r'$env:USERPROFILE\.emrg\install\git\*' in content
    # pass 1：先杀 git 树（git/ssh/plink/bash）
    assert "-in @('git.exe', 'ssh.exe', 'plink.exe', 'bash.exe')" in content
    # pass 2：连坐强杀——前缀内全部残留（sh/vim 一并杀，宿主拍板）
    # 判别：第二次 Get-CimInstance 无 Name 过滤（不再区分工具类型）
    assert content.count("Get-CimInstance Win32_Process") >= 3
    pass2_idx = content.index("# pass 2")
    assert "Stop-Process -Id $_.ProcessId -Force" in content
    # 退出码语义：无残留（或本来无进程）→ 0；仍有存活 → 1
    assert "exit 1" in content and "exit 0" in content
    assert 'if ($left.Count -gt 0) { exit 1 } else { exit 0 }' in content
    # 独立脚本 + 非交互（无 cmd 内联转义问题，0.2.26 转义 bug 根因规避）
    assert "$ErrorActionPreference = 'SilentlyContinue'" in content


def test_emrgd_cmd_has_stop_branch():
    content = (REPO_ROOT / "bin" / "emrgd.cmd").read_text(encoding="utf-8")
    assert 'if /I not "%~1"=="stop" goto :start' in content
    assert "-m emrg server stop" in content
    assert "exit /b %errorlevel%" in content


def test_make_installer_iss_has_prepare_to_install():
    content = (REPO_ROOT / "packaging" / "make-installer.sh").read_text(encoding="utf-8")
    assert "dontcopy" in content
    assert "stop-emrg.cmd" in content
    assert "stop-git.ps1" in content
    assert "PrepareToInstall" in content
    assert "ExtractTemporaryFile('stop-emrg.cmd')" in content
    assert "ExtractTemporaryFile('stop-git.ps1')" in content
    # {tmp} 提取版 stop-git.ps1 作为 %~1 传给 stop-emrg.cmd（旧安装可能无此文件）
    assert "GitStopScript" in content
    assert "'/c \"' + StopScript + '\" \"' + GitStopScript" in content
    assert "SW_HIDE" in content  # 批处理执行不弹控制台窗口（#592 纪律）
    # rant 2026-08-11T17:56:25：中止消息含重启兜底引导（杀不掉时宿主可重启后重试）
    assert "restart the computer" in content


def test_build_runtime_copies_stop_scripts():
    content = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/bin/stop-emrg.cmd" stop-emrg.cmd' in content
    assert 'cp "$ROOT/bin/stop-git.ps1" stop-git.ps1' in content
