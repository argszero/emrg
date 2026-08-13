"""Windows 安装器预停止接线回归测试（rant 2026-08-10T08:50:44 + 宿主 2026-08-11T19:47:44 连坐强杀拍板
+ rant 2026-08-12T14:00:05 合并单文件）。

安装器（Inno Setup，make-installer.sh 生成 emrg.iss）覆盖 ~/.emrg/install 前必须
先优雅关闭 GUI/TUI/daemon/bundled-git，否则无窗口的 pythonw daemon 独占锁文件 → 卡在
"停止已有进程"（宿主只能重启系统）。本测试纯文本断言（不执行 iscc/cmd ——
macOS/CI 无 Windows），钉死接线：
  1. bin/stop-emrg.cmd 单文件覆盖全流程：GUI 优雅关闭+/F 兜底、TUI 命令行过滤、
     daemon 协议关闭 + emrgd.pid 轮询兜底（顺序 GUI → daemon）、step 4 内联
     PowerShell 连坐强杀 bundled git（stop-git.ps1 已删除合并，rant 2026-08-12T14:00:05）
  2. bin/stop-git.ps1 不存在；内联逻辑：install\\git\\ 前缀收集 → 先杀 git 树 →
     连坐强杀全部残留（sh/vim 一并杀）→ 每次枚举最新 Get-CimInstance → 无残留
     exit 0 / 有残留 exit 1（truthful failure，#701）
  3. bin/emrgd.cmd 含 stop 分支（复用 `emrg server stop`）
  4. make-installer.sh 的 .iss 模板含 [Files] dontcopy（stop-emrg.cmd）+ [Code]
     PrepareToInstall 运行 {tmp} 提取版
  5. build-runtime.sh 把 stop-emrg.cmd 复制进 runtime bin/
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_stop_emrg_cmd_covers_gui_tui_daemon_and_inline_step4():
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
    # step 4（rant 2026-08-12T14:00:05 合并单文件）：内联 PowerShell 连坐强杀。
    # 判别：-Command 内联（无 -File 独立脚本），\" 转义与 TUI/verify 段同模式。
    step4_idx = content.index("\n:step4\n")  # v2 label（宿主 2026-08-13 真机验证版：标签结构替代注释头）
    verify_idx = content.index("\n:verify\n")  # 标签定义处（\n 前缀排除前面 goto :verify 行）
    assert daemon_line < step4_idx < verify_idx
    assert "-ExecutionPolicy Bypass -Command" in content
    assert "-ExecutionPolicy Bypass -File" not in content  # 独立脚本已删除
    assert "GITSTOP" not in content  # %~1/{tmp} 传递机制随 stop-git.ps1 一并移除
    # pass 1：先杀 git 树（git/ssh/plink/bash）
    assert "-in @('git.exe','ssh.exe','plink.exe','bash.exe')" in content
    # 前缀过滤：只碰 %USERPROFILE%\.emrg\install\git\*（系统 Git 永不命中）；
    # cmd 内联转义形态（\" 包裹 PS 字符串字面量）
    assert r'\"$env:USERPROFILE\.emrg\install\git\*\"' in content
    # 存活检查基于最新枚举（Get-CimInstance 每次重新查询，无旧快照）
    assert content.count("Get-CimInstance Win32_Process") >= 4  # pass1 + pass2 + $left + verify
    # truthful failure：幸存进程点名输出 + exit 1（rant 2026-08-12T12:30:41 语义）
    assert r'Write-Host (\"still running: {0} (pid {1})\" -f $_.Name, $_.ProcessId)' in content
    # step 4 失败必须传导到退出码（EXIT_CODE 初始化在 setlocal 之后、verify 不再清零）
    assert 'set "EXIT_CODE=0"' in content
    assert 'if errorlevel 1 set "EXIT_CODE=1"' in content
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
    assert 'if errorlevel 1 set "EXIT_CODE=1"' in verify_block
    assert r'$env:USERPROFILE\.emrg\install\git\*' in verify_block
    assert "Get-CimInstance" in verify_block


def test_stop_git_merged_single_file():
    # rant 2026-08-12T14:00:05 验收：bin/ 下无 stop-git.ps1；grep stop-git 仅历史注释
    assert not (REPO_ROOT / "bin" / "stop-git.ps1").exists()
    cmd = (REPO_ROOT / "bin" / "stop-emrg.cmd").read_text(encoding="utf-8")
    step4 = cmd[cmd.index("\n:step4\n"):cmd.index("\n:verify\n")]
    # pass 2：连坐强杀——前缀内全部残留（sh/vim 一并杀，宿主 2026-08-11T19:47:44 拍板）
    assert "Start-Sleep -Milliseconds 300" in step4
    # 每次枚举重新 Get-CimInstance（不用旧快照）——pass1/pass2/$left 共 3 次
    assert step4.count("Get-CimInstance Win32_Process") == 3
    # 退出码语义：无残留（或本来无进程）→ 0；仍有存活 → 1
    assert "exit 1" in step4 and "exit 0" in step4
    # 打包链路无功能引用：make-installer.sh / build-runtime.sh 不再处理 stop-git.ps1
    mi = (REPO_ROOT / "packaging" / "make-installer.sh").read_text(encoding="utf-8")
    assert '"$STAGE_WIN/payload\\\\bin\\\\stop-git.ps1"' not in mi
    assert "ExtractTemporaryFile('stop-git.ps1')" not in mi
    assert "GitStopScript" not in mi
    br = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/bin/stop-git.ps1"' not in br


def test_stop_emrg_v2_step4_always_runs():
    """R127 / rant 2026-08-13T09:56:47 + 10:00:33 — host-verified v2 (EXIT_CODE=0):
    step 4 (kill bundled git) must ALWAYS run before :verify. The old
    `if not exist emrgd.pid goto :verify` skipped step 4 when the daemon was
    down → orphaned evolution-spawned git/sh/vim processes locked install\\git
    → verify reported them → exit 1 → installer aborted."""
    content = (REPO_ROOT / "bin" / "stop-emrg.cmd").read_text(encoding="utf-8")
    # step 4 unconditional: daemon section falls through via goto :step4, never to :verify
    daemon_end = content.index("goto :step4")
    verify_idx = content.index("\n:verify\n")
    step4_idx = content.index("\n:step4\n")
    assert daemon_end < step4_idx < verify_idx
    # no direct daemon→verify jump exists (only the :verify label, no early goto :verify)
    # (v1 had `if not exist "%EMRG_DIR%\\emrgd.pid" goto :verify` — must be gone)
    assert 'goto :verify' not in content
    # paren-block %VAR% parse-time expansion fixed: !TRIES! delayed expansion for the loop guard
    assert "if !TRIES! geq 10 goto :kill_pid" in content
    # label structure replaces nested parens for TUI (wmic/PowerShell) + daemon stop
    assert ":tui_wmic" in content and ":tui_done" in content
    assert ":daemon_stop" in content and ":daemon_pid" in content
    # host diagnostics preserved (每步 echo [N] check/kill/result)
    assert "echo [4] check+kill bundled git" in content
    assert "echo [verify] git residual" in content


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
    # 单文件：只提取/执行 stop-emrg.cmd（stop-git.ps1 已删除）
    assert "ExtractTemporaryFile('stop-git.ps1')" not in content
    # R125: rant 2026-08-13T09:24:37 — 输出重定向到 {tmp}\stop-emrg.log（2>&1），
    # 失败时 LoadStringFromFile 读日志展示杀不掉的进程，不再让宿主手动跑诊断
    assert '/c ""\' + StopScript + \'" > "\' + LogFile + \'" 2>&1"' in content
    # ⚡ LoadStringFromFile 的 Inno Pascal Script 签名是 2 参数 out-param 形式
    # `(const FileName: String; var S: AnsiString): Boolean`（6.7.1 → 7.x 一致，
    # issrc Shared.ScriptFunc.pas）——单参数字符串返回形式不存在，iscc 编译报
    # "Invalid number of parameters"（v0.2.30 Build Release 31661378619 实际失败，
    # Test CI 不编译 .iss 未拦住）。正反两态钉死正确调用形态。
    assert "LoadStringFromFile(LogFile, LogText)" in content  # 正：out-param 形式
    assert ":= LoadStringFromFile(LogFile)" not in content  # 反：1 参数形式不存在
    # ⚡ 2 参形式第 2 参是 var S: AnsiString——LogText 必须声明 AnsiString（Inno 6
    # 的 string=UnicodeString，传 string 变量 → iscc "Type mismatch"，门禁实测拦截）。
    assert "LogText: AnsiString;" in content  # 正：AnsiString 变量
    assert "LogText: string;" not in content  # 反：UnicodeString 不匹配 var AnsiString
    assert "Length(LogText) > 2000" in content
    assert "Details from stop-emrg.cmd:" in content
    assert "SW_HIDE" in content  # 批处理执行不弹控制台窗口（#592 纪律）
    # rant 2026-08-11T17:56:25：中止消息含重启兜底引导（杀不掉时宿主可重启后重试）
    assert "restart the computer" in content


def test_build_runtime_copies_stop_scripts():
    content = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/bin/stop-emrg.cmd" stop-emrg.cmd' in content
    assert 'cp "$ROOT/bin/stop-git.ps1"' not in content


def test_build_runtime_forces_crlf():
    """R126 / rant 2026-08-13T09:44:32 — build-runtime.sh must force pure CRLF
    on the packaged *.cmd files (git blob is LF; .gitattributes checkout
    conversion may not apply in CI → LF .cmd misparsed by cmd.exe → installer
    "exit code 1")."""
    content = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    # Normalization loop covers all three Windows launchers
    assert "for f in emrg.cmd emrgd.cmd stop-emrg.cmd" in content
    # Pure-CRLF assertion gates the build (LF-only → build fails)
    assert "b.count(b'\\r\\n') == b.count(b'\\n')" in content


def test_build_release_workflow_verifies_crlf():
    """R126 companion — build-release.yml must assert packaged *.cmd are pure
    CRLF after the Build runtime step (CI red on LF)."""
    wf = (REPO_ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
    assert "Verify runtime *.cmd are pure CRLF" in wf
    assert "bare-LF" in wf
