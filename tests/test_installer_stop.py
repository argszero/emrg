"""Windows 安装器预停止接线回归测试（rant 2026-08-10T08:50:44 → 2026-08-17T10:32:27 收敛）。

安装器（Inno Setup，make-installer.sh 生成 emrg.iss）覆盖 ~/.emrg/install 前必须
先优雅关闭 GUI/TUI/daemon/bundled-git，否则无窗口的 pythonw daemon 独占锁文件 → 卡在
"停止已有进程"（宿主只能重启系统）。宿主 2026-08-17 拍板：**停止逻辑全部收敛到
Python**——bin/stop-emrg.cmd 删除，新增 emrg/_stop_all.py（纯标准库单文件），
emrg stop 与 Inno PrepareToInstall 共用同一实现。本测试纯文本断言（不执行
iscc/cmd/python —— macOS/CI 无 Windows），钉死接线：
  1. bin/stop-emrg.cmd 已删除；bin/stop-git.ps1 仍不存在
  2. emrg/_stop_all.py 覆盖全流程：ws 协议关闭 → emrgd.pid 兜底 → taskkill /F、
     GUI 优雅关闭+/F 兜底、TUI CIM 命令行过滤（python.exe|pythonw.exe）、
     install\\git\\ 前缀连坐强杀 bundled git、verify 残留检查 + exit 1
  3. emrg/__main__.py 的 stop 子命令 sys.exit(_stop_all())（退出码透传）
  4. make-installer.sh 的 .iss 模板：[Files] dontcopy(stop_all.py) + [Code]
     PrepareToInstall 用 runtime python 运行 {tmp} 提取版
  5. build-runtime.sh 把 emrg/_stop_all.py 复制进 runtime bin/stop_all.py
     （stop-emrg.cmd 不再打包；CRLF 转换循环只剩 emrg.cmd emrgd.cmd）
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_stop_emrg_cmd_deleted():
    # 宿主 2026-08-17T10:32:27：不要 stop-emrg.cmd 了
    assert not (REPO_ROOT / "bin" / "stop-emrg.cmd").exists()
    assert not (REPO_ROOT / "bin" / "stop-git.ps1").exists()


def test_stop_all_py_covers_daemon_gui_tui_git_verify():
    content = _read("emrg/_stop_all.py")
    # daemon：ws 协议关闭 + emrgd.pid 兜底（taskkill /F）
    assert "ws_graceful_shutdown" in content
    assert "emrgd.pid" in content
    assert '["taskkill", "/F", "/PID", str(pid)]' in content
    # GUI：优雅 taskkill /IM EMRG.exe 先于无条件 /F（宿主 01:27:07Z 教训）
    assert '"taskkill", "/IM", "EMRG.exe"' in content
    assert '"taskkill", "/F", "/IM", "EMRG.exe"' in content
    assert content.index('"/IM", "EMRG.exe"') < content.index('"/F", "/IM", "EMRG.exe"')
    # TUI：CIM 命令行过滤 python.exe|pythonw.exe，排除 emrg.server
    assert r"python(\\.exe|w\\.exe)?" in content
    assert r"-notmatch 'emrg\\.server'" in content
    # 调用方自身 PID 排除（`emrg stop` CLI 本身匹配 -m emrg 过滤，会自杀于
    # stop_bundled_git + verify 之前 — pm25coder #811 review finding 1）
    assert r"$_.ProcessId -ne {own}" in content
    assert ".format(own=own)" in content
    # bundled git：install\\git\\ 前缀（系统 Git 永不命中）
    assert r'\"$env:USERPROFILE\\.emrg\\install\\git\\*\"' in content
    assert "stop_bundled_git" in content
    # verify + 非零退出码（残留清单点名进程名+pid）
    assert "def verify" in content
    assert "return 1" in content
    assert "WARNING residual process(es) still running" in content
    # 纯标准库：import 行不得引用 emrg 包（安装器独立运行）
    import_lines = [ln for ln in content.splitlines()
                    if ln.startswith(("import ", "from "))]
    assert import_lines, "no import lines found"
    assert all(not ln.startswith(("from emrg", "import emrg")) for ln in import_lines)
    assert "__name__ == \"__main__\"" in content


def test_stop_all_py_cmdline_scan_fallback():
    """rant 2026-08-17T17:03:38 — DeleteFile code 5: pid 文件盲区兜底。

    stop_daemon() 只杀 emrgd.pid 里的 pid（文件丢失/过时/不匹配 → 实际活着的
    pythonw daemon 漏杀，锁住 websockets C 扩展），stop_tui() 刻意排除
    emrg.server，verify() 不扫 python 进程 → 漏杀时 exit 0 → Inno 继续覆盖。
    修复 = Windows 侧按命令行扫描 python.exe|pythonw.exe -m emrg(.server)
    兜底（cmdline 是唯一可靠身份），daemon 步与 verify 步都接入。
    """
    content = _read("emrg/_stop_all.py")
    # 扫描辅助：python.exe|pythonw.exe + CommandLine 匹配 -m emrg（含 emrg.server），
    # 排除自身；不排除 emrg.server（那是 stop_tui 的盲区）
    assert "def _scan_windows_python_emrg" in content
    assert r"python(\\.exe|w\\.exe)?" in content
    assert r"-match '-m emrg'" in content
    assert "Write-Output $_.ProcessId" in content
    assert "emrg\\.server" not in content  # 绝不能 -notmatch emrg.server
    # stop_daemon() 在 pid 路径后追加 cmdline 兜底
    daemon_src = content.split("def stop_daemon")[1].split("def stop_gui")[0]
    assert "_scan_windows_python_emrg(os.getpid())" in daemon_src
    assert "_kill_pid_windows(pid)" in daemon_src
    # verify() 增加 python emrg 进程残留检查（不依赖 pid 文件）
    verify_src = content.split("def _verify_windows")[1].split("def _verify_posix")[0]
    assert "_scan_windows_python_emrg(os.getpid())" in verify_src
    assert 'residuals.append(f"python emrg process (pid {pid})")' in verify_src


def test_stop_all_py_restart_manager_lock_owners():
    """rant 2026-08-17T17:55:42 — DeleteFile code 5 通用解（Restart Manager）。

    0.2.43 安装实测根因：占用 install\\ 下文件的是【外来进程】（browser-harness
    daemon，独立 uv CPython，AppData\\Roaming\\uv\\tools），emrgd.pid/emrgd.port
    全空、无任何 -m emrg 进程 → 命令行扫描永远找不到。修复 = Restart Manager
    （rstrtmgr.dll）扫 install\\ 全部文件收集占用者 → 排除自身+祖先进程链
    （stop_all 由 install\\python-dist\\python.exe 执行，自身加载 install\\python313.dll；
    祖先含 Inno setup.exe 绝不能杀）→ Stop-Process -Force，打印 PID/名称/命令行
    （截断 150）；verify 用同一扫描复查残留 → exit 1。
    """
    content = _read("emrg/_stop_all.py")
    # 扫描+击杀步骤与辅助
    assert "def stop_lock_owners" in content
    assert "def _lock_owner_ps" in content
    assert "def _windows_lock_owners" in content
    # Restart Manager API + 批注册 + ERROR_MORE_DATA(234) 重试
    assert "rstrtmgr.dll" in content
    assert "RmRegisterResources" in content
    assert "RmGetList" in content
    assert "234" in content
    # 不硬编码用户名 + 祖先链排除 + 命令行截断 150 + 击杀 + browser-harness 提示
    assert "$env:USERPROFILE" in content
    assert "ParentProcessId" in content
    assert "Substring(0, 150)" in content
    assert "Stop-Process" in content
    assert "browser" in content
    # stop_all() 顺序：bundled git 之后、verify 之前
    stop_all_src = content.split("def stop_all")[1]
    assert "stop_bundled_git()" in stop_all_src
    assert "stop_lock_owners()" in stop_all_src
    assert stop_all_src.index("stop_bundled_git()") < stop_all_src.index("stop_lock_owners()")
    # verify 接入：残留 file-lock owner → 点名 + exit 1（R125 中止语义不变）
    verify_src = content.split("def _verify_windows")[1].split("def _verify_posix")[0]
    assert "_windows_lock_owners(kill=False, stdout=rm_out)" in verify_src
    assert "file-lock owner" in verify_src
    # rant 2026-08-17T21:04:32：verify 也要打印 RM 扫描摘要（防静默空转）
    assert "_print_rm_diag(rm_out)" in verify_src


def test_main_delegates_stop_to_stop_all():
    content = _read("emrg/__main__.py")
    # stop 子命令帮助文案不再引用 stop-emrg.cmd
    assert "stop-emrg.cmd" not in content
    # main() 对 stop 分支 sys.exit(_stop_all())；_stop_all 委托 emrg._stop_all
    assert "sys.exit(_stop_all())" in content
    assert "from emrg._stop_all import stop_all" in content


def test_emrgd_cmd_has_stop_branch():
    content = _read("bin/emrgd.cmd")
    assert 'if /I not "%~1"=="stop" goto :start' in content
    assert "-m emrg server stop" in content
    assert "exit /b %errorlevel%" in content


def test_make_installer_iss_has_prepare_to_install():
    content = _read("packaging/make-installer.sh")
    # 无任何功能引用 stop-emrg.cmd（历史注释里的"删除"说明除外）
    assert "payload\\\\bin\\\\stop-emrg.cmd" not in content
    assert "ExtractTemporaryFile('stop-emrg.cmd')" not in content
    assert "stop-emrg.log" not in content
    # dontcopy 只提取/执行 stop_all.py
    assert "dontcopy" in content
    assert "payload\\\\bin\\\\stop_all.py" in content
    assert "PrepareToInstall" in content
    assert "ExtractTemporaryFile('stop_all.py')" in content
    assert "ExtractTemporaryFile('stop-git.ps1')" not in content
    # runtime python 探测链（R90 布局，与 emrg.cmd 一致）；干净安装跳过
    assert "python-dist\\python.exe" in content
    assert "python-dist\\python3.13.exe" in content
    assert "if not FileExists(PythonExe) then" in content
    # R125: rant 2026-08-13T09:24:37 — 输出重定向到 {tmp}\stop_all.log（2>&1），
    # 失败时 LoadStringFromFile 读日志展示杀不掉的进程
    assert "stop_all.log" in content
    assert '''" > "' + LogFile + '" 2>&1"''' in content
    assert content.count("2>&1") >= 1
    # ⚡ LoadStringFromFile 的 Inno Pascal Script 签名是 2 参数 out-param 形式
    assert "LoadStringFromFile(LogFile, LogText)" in content  # 正：out-param 形式
    assert ":= LoadStringFromFile(LogFile)" not in content  # 反：1 参数形式不存在
    assert "LogText: AnsiString;" in content  # 正：AnsiString 变量
    assert "LogText: string;" not in content  # 反：UnicodeString 不匹配
    assert "Length(LogText) > 2000" in content
    assert "Details from the stop script:" in content
    assert "SW_HIDE" in content  # 不弹控制台窗口（#592 纪律）
    # 中止消息含重启兜底引导
    assert "restart the computer" in content


def test_build_runtime_copies_stop_all_not_stop_emrg():
    content = _read("packaging/build-runtime.sh")
    # 复制 emrg/_stop_all.py → bin/stop_all.py（不再复制 stop-emrg.cmd）
    assert 'cp "$ROOT/emrg/_stop_all.py" stop_all.py' in content
    assert 'cp "$ROOT/bin/stop-emrg.cmd"' not in content
    # R126 CRLF 转换循环只剩两个 .cmd 启动器
    assert "for f in emrg.cmd emrgd.cmd" in content
    assert "stop-emrg.cmd" not in content.split("for f in emrg.cmd emrgd.cmd")[1]
    # Pure-CRLF assertion gate keeps guarding the build
    assert "b.count(b'\\r\\n') == b.count(b'\\n')" in content


def test_build_release_workflow_verifies_crlf():
    wf = _read(".github/workflows/build-release.yml")
    assert "Verify runtime *.cmd are pure CRLF" in wf
    assert "bare-LF" in wf


def test_test_workflow_iscc_stub_has_stop_all():
    wf = _read(".github/workflows/test.yml")
    assert 'touch "$STAGE/payload/bin/stop_all.py"' in wf
    assert "stop-emrg.cmd" not in wf


def test_agent_md_no_stop_emrg_cmd_refs():
    """No stale stop-emrg.cmd references in docs."""
    for rel in ("README.md", "README.cn.md", "Agent.md"):
        assert "stop-emrg.cmd" not in _read(rel), rel
