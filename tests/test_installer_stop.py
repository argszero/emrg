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
