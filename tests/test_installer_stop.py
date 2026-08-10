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
    # TUI：命令行过滤（wmic LIKE 通配符须 %% 转义）
    assert "wmic" in content and "commandline like" in content
    assert "powershell" in content  # wmic 缺失（Win11 24H2+）时的回退
    # daemon：协议关闭 + pid 轮询兜底（用实际执行行而非注释里的字面量）
    assert 'call "%INSTALL%\\bin\\emrg.cmd" server stop' in content
    assert "emrgd.pid" in content
    # 顺序：GUI 在 daemon 之前（GUI 不能复活 daemon）
    daemon_line = content.index('call "%INSTALL%\\bin\\emrg.cmd" server stop')
    assert content.index("taskkill /IM EMRG.exe") < daemon_line
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


def test_build_runtime_copies_stop_emrg_cmd():
    content = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
    assert 'cp "$ROOT/bin/stop-emrg.cmd" stop-emrg.cmd' in content
