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
  6. PrepareToInstall 旧 python 健康检查（rant 2026-08-19T00:44:52）：
     stop_all 前先 -c "import encodings, sys" 验证旧 runtime python 可启动，
     损坏（encodings 崩）→ 跳过 stop_all 继续安装（不硬中止升级）
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
    # TUI：CIM 命令行过滤 python 解释器镜像（python.exe|pythonw.exe|python3.13.exe
    # 等版本化启动器，_WIN_PY_NAME_RE 宽松匹配），排除 emrg.server
    assert "_WIN_PY_NAME_RE" in content
    assert r'^python.*\.exe$' in content
    assert r"-notmatch 'emrg\\.server'" in content
    # 调用方自身 PID 排除（`emrg stop` CLI 本身匹配 -m emrg 过滤，会自杀于
    # stop_bundled_git + verify 之前 — pm25coder #811 review finding 1）
    assert r"$_.ProcessId -ne {own}" in content
    assert ".format(own=own, name_re=_WIN_PY_NAME_RE)" in content
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
    # 扫描辅助：python 解释器镜像（_WIN_PY_NAME_RE 宽松匹配版本化启动器）
    # + CommandLine 匹配 -m emrg（含 emrg.server），排除自身；不排除
    # emrg.server（那是 stop_tui 的盲区）
    assert "def _scan_windows_python_emrg" in content
    assert "_WIN_PY_NAME_RE" in content
    assert r'^python.*\.exe$' in content
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
    assert 'f"python emrg process (pid {p})"' in verify_src


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
    # rant 2026-08-18T16:09:45 — kill 版 RM 用 taskkill /F /PID（TerminateProcess
    # 立即返回）替代 Stop-Process：PowerShell Stop-Process 对拒绝/等待场景挂起
    # → kill 版 RM 扫描 60s 超时（Stop-Process 挂起）实证见 v0.2.48 stop_all.log。
    assert "& taskkill /F /PID $pid" in content
    # stop_all() 顺序：bundled git 之后、verify 之前（步骤计划表 _step_plan，
    # rant 2026-08-17T21:06:31 日志规范重构后 stop_all 从计划表驱动）
    step_src = content.split("def _step_plan")[1].split("def stop_all")[0]
    assert "stop_bundled_git" in step_src
    assert "stop_lock_owners" in step_src
    assert step_src.index("stop_bundled_git") < step_src.index("stop_lock_owners")
    assert '"GUI", stop_gui' in step_src
    assert '"daemon", stop_daemon' in step_src
    # verify 接入：残留 file-lock owner → 点名 + exit 1（R125 中止语义不变）
    verify_src = content.split("def _verify_windows")[1].split("def _verify_posix")[0]
    assert "_windows_lock_owners(kill=False, stdout=rm_out)" in verify_src
    assert "file-lock owner" in verify_src
    # rant 2026-08-17T21:04:32：verify 也要打印 RM 扫描摘要（防静默空转）
    assert "_print_rm_diag(rm_out)" in verify_src


def test_stop_all_py_deletefile_semantic_lock_probe():
    """rant 2026-08-18T16:09:45 — lock-probe 假阴性根因 + 修复；
    rant 2026-08-19T13:08:41 — 数据删除 bug 修复（去掉 FILE_FLAG_DELETE_ON_CLOSE）。

    v0.2.48 实证：GENERIC_READ + FILE_SHARE_NONE 探测对 DLL 锁永远假阴性——
    LoadLibrary 持有句柄允许读共享 → 探测显示 0 locked，而安装器 DeleteFile
    需要句柄共享 FILE_SHARE_DELETE → 覆盖时 code 5。修复 = DELETE 访问
    （GENERIC_DELETE=0x10000）+ OPEN_EXISTING + FILE_SHARE_NONE，与安装器
    DeleteFile 的共享语义一致；探测成功即"DeleteFile 会成功"，仅关闭句柄，
    永不设置删除 disposition。

    ⚠️ 数据删除 bug（rant 2026-08-19T13:08:41）：旧实现用
    FILE_FLAG_DELETE_ON_CLOSE=0x04000000 打开后再用 SetFileInformationByHandle
    清除 disposition——但该清除仅 Windows 10 1903+ 支持，旧系统/清除失败时
    disposition 残留，CloseHandle 会真删文件。现探测用普通
    FILE_ATTRIBUTE_NORMAL 打开，永不设置 disposition → 只问不删。
    """
    content = _read("emrg/_stop_all.py")
    assert "GENERIC_DELETE = 0x00010000" in content
    assert "GENERIC_READ = 0x80000000" not in content  # 旧常量赋值已移除
    # 数据删除 bug 修复：不得再出现 delete-on-close / disposition 清除
    assert "FILE_FLAG_DELETE_ON_CLOSE" not in content
    assert "SetFileInformationByHandle" not in content
    assert "FILE_DISPOSITION_INFO" not in content
    assert "FILE_ATTRIBUTE_NORMAL = 0x80" in content
    assert "never sets a delete disposition" in content or "never set a delete disposition" in content
    # 自锁防护（rant 2026-08-18T16:09:45，18:57:09 改为提示性）：开头打印
    # python-dist 运行时 + verify 对 self-held 锁不中止安装（stop_all 退出即释放）
    assert "python-dist runtime:" in content
    assert "self-held" in content
    assert "installer continues" in content
    assert "re-run installer (fresh process won't hold the lock)" not in content  # 旧文案已移除


def test_stop_all_py_rm_no_external_owner_flag():
    """rant 2026-08-18T16:09:45 — _print_rm_diag 记录"无外部 owner"证据。

    RM owners==0 或全部 owner 被祖先链排除 → _rm_no_external_owner=True，
    stop_all 重试循环后据此输出自锁提示（18:57:09 改为 advisory，不再 exit 1）。
    """
    content = _read("emrg/_stop_all.py")
    assert "_rm_no_external_owner" in content
    assert 'd["owners"] == 0 or "owner(s) excluded" in stdout' in content
    assert "installer continues" in content


def test_stop_all_py_module_holder_enumeration():
    """rant 2026-08-18T16:24:01 — 诊断脚本铁证：DLL 模块锁只有
    Process.Modules 枚举能点名（RM 漏报 browser-harness 子进程 PID 9280；
    CreateFileW 探测 DELETE+SHARE_NONE OK 但 DeleteFile 仍失败）。

    find_install_module_holders(): Get-Process + $_.Modules.FileName 过滤
    install 前缀 → 输出 holder<TAB>pid<TAB>name<TAB>exe<TAB>parent<TAB>files<TAB>tag
    （祖先链排除 tag=excluded）；stop_lock_owners 先跑 module-holder 枚举，
    对 target 用 taskkill /F /T /PID（进程树击杀），browser-harness hint；
    verify 的 module-holder 分类把外部持有者列为残留。
    """
    content = _read("emrg/_stop_all.py")
    # 枚举 + 解析 + 击杀辅助
    assert "def find_install_module_holders" in content
    assert "def _parse_module_holders" in content
    assert "def _kill_tree_windows" in content
    # PowerShell 核心：Modules.FileName 过滤 install 前缀 + 祖先链排除
    assert "Get-Process" in content
    assert '$_.Modules.FileName' in content
    assert "like \"$root\\*\"" in content or 'like "$root\\*"' in content
    assert "ParentProcessId" in content
    assert "holder`t" in content
    # 击杀用 taskkill /F /T /PID（进程树，TerminateProcess 立即返回）
    assert '["taskkill", "/F", "/T", "/PID", str(pid)]' in content
    # stop_lock_owners 先 module-holder 后 RM；browser-harness hint
    stop_src = content.split("def stop_lock_owners")[1].split("# ── Verify")[0]
    assert "find_install_module_holders()" in stop_src
    assert "_lock_owner_ps(kill=True)" in stop_src
    assert "restart it after the installer completes" in stop_src
    # verify：module-holder 分类（主检测）在 RM re-scan 之前
    verify_src = content.split("def _verify_windows_categories")[1].split("def _verify_windows_summary")[0]
    assert '"module-holder", mh' in verify_src
    assert '"RM re-scan", rm' in verify_src
    assert verify_src.index('"module-holder"') < verify_src.index('"RM re-scan"')
    assert "install-module holder" in verify_src
    # 自锁兜底：外部 module-holder 与 RM owner 都无 → 提示性 WARNING（18:57:09
    # 改为 advisory —— stop_all 退出即释放，安装继续）
    assert "_module_holder_external_found" in content
    assert "installer continues" in content
    # createfile-probe 降级为补充（CreateFileW 探测对 DLL 模块锁假阴性）
    assert "createfile-probe" in content
    assert "module locks need the module-holder scan" in content


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
    # rants 2026-08-18T21:13:03 / 21:14:16 — 移除 -I isolated mode：
    # python-build-standalone 靠目录结构 / ._pth 定位 stdlib，-I 忽略之 →
    # v0.2.50 Windows 安装 "Failed to import encodings" 启动即崩（无 stop_all.log）。
    # -I 防的"自锁"假设被证伪：excluded-chain 已排除自身 + 祖先进程链（从不杀自己），
    # #847 self-held 归属已处理自身锁，且 stop_all 纯标准库从不加载 websockets。
    # 恢复 v0.2.49 的 "{PythonExe}" "{StopScript}"，无替代 flag。
    assert '''""' + PythonExe + '" "' + StopScript''' in content
    assert '''""' + PythonExe + '" -I "' + StopScript''' not in content
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


def test_make_installer_iss_old_python_health_check():
    """rant 2026-08-19T00:44:52 — 安装器韧性：旧版 python-dist 损坏（encodings 崩，
    v0.2.49/v0.2.50 -I 事故遗留）时，升级安装不得因坏 python 硬中止。

    宿主实测：v0.2.51 升级报 `ModuleNotFoundError: No module named 'encodings'`
    （连 stop_all.log 都没有，启动即崩）；删除旧 install\\bin 后走干净安装分支
    即成功 —— 包本身完好，脆弱点在 PrepareToInstall 用旧 python 跑 stop_all。
    修复 = 跑 stop_all 前先健康检查旧 python 可启动（-c "import encodings, sys"），
    失败 → 跳过 stop_all（干净安装路径，不中止）。
    """
    content = _read("packaging/make-installer.sh")
    # 健康检查命令与 stop_all 同款：经 {cmd} 重定向到日志（2>&1）
    assert '''""' + PythonExe + '" -c "import encodings, sys"''' in content
    assert content.count("2>&1") >= 2  # 健康检查 + stop_all 两处 Exec 重定向
    # 失败 → 跳过 stop_all 继续安装（Result 保持 '' → 不中止），信息进日志
    assert "skipping stop_all" in content
    assert "health check" in content
    assert "Log('PrepareToInstall: old runtime python failed health check" in content
    # 健康检查必须先于 stop_all 执行
    assert content.index("import encodings, sys") < content.index('''""' + PythonExe + '" "' + StopScript''')
    # 健康检查通过后仍保留原 stop_all 调用
    assert '''""' + PythonExe + '" "' + StopScript''' in content


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


def test_classify_locked_files_self_held_only():
    """rant 2026-08-18T18:57:09 — python-dist DLLs locked by stop_all's own
    runtime (module-holder tag=excluded) are self-held → NOT residuals."""
    from emrg._stop_all import _classify_locked_files

    root = "C:\\Users\\x\\.emrg\\install"
    locked = [
        root + "\\bin\\python-dist\\python313.dll",
        root + "\\bin\\python-dist\\select.pyd",
    ]
    holders = [
        (11572, "python.exe", "python-dist", 1,
         ["bin/python-dist/python313.dll", "bin/python-dist/select.pyd"], "excluded"),
    ]
    self_held, residual = _classify_locked_files(locked, holders, root)
    assert sorted(self_held) == ["bin/python-dist/python313.dll", "bin/python-dist/select.pyd"]
    assert residual == []


def test_classify_locked_files_external_holder_residual():
    """A locked file held by an EXTERNAL (target) module-holder stays residual."""
    from emrg._stop_all import _classify_locked_files

    root = "C:\\Users\\x\\.emrg\\install"
    locked = [root + "\\lib\\websockets\\speedups.cp313-win_amd64.pyd"]
    holders = [
        (9280, "python.exe", "browser_harness", 9556,
         ["lib/websockets/speedups.cp313-win_amd64.pyd"], "target"),
    ]
    self_held, residual = _classify_locked_files(locked, holders, root)
    assert self_held == []
    assert residual == ["lib/websockets/speedups.cp313-win_amd64.pyd"]


def test_classify_locked_files_unattributable_residual_conservative():
    """A locked file with NO known module-holder stays residual (conservative —
    could be a plain non-DLL lock held by an external process)."""
    from emrg._stop_all import _classify_locked_files

    root = "C:\\Users\\x\\.emrg\\install"
    locked = [root + "\\bin\\some-data-file.dat"]
    self_held, residual = _classify_locked_files(locked, [], root)
    assert self_held == []
    assert residual == ["bin/some-data-file.dat"]


def test_classify_locked_files_mixed():
    """Mixed: self-held python-dist + external pyd + unattributable data."""
    from emrg._stop_all import _classify_locked_files

    root = "C:\\Users\\x\\.emrg\\install"
    locked = [
        root + "\\bin\\python-dist\\python313.dll",
        root + "\\lib\\websockets\\speedups.cp313-win_amd64.pyd",
        root + "\\bin\\data.dat",
    ]
    holders = [
        (11572, "python.exe", "python-dist", 1,
         ["bin/python-dist/python313.dll"], "excluded"),
        (9280, "python.exe", "browser_harness", 9556,
         ["lib/websockets/speedups.cp313-win_amd64.pyd"], "target"),
    ]
    self_held, residual = _classify_locked_files(locked, holders, root)
    assert self_held == ["bin/python-dist/python313.dll"]
    assert sorted(residual) == [
        "bin/data.dat",
        "lib/websockets/speedups.cp313-win_amd64.pyd",
    ]
