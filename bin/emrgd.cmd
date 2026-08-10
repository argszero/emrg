@echo off
REM EMRG daemon launcher — installed at %USERPROFILE%\.emrg\install\bin\emrgd.cmd
REM Phase 4 installer (rant #12) §2 — same structure as bin\emrg.cmd; entry is
REM the server package. GUI main.js spawns this path (R36 shell:true, R66 windowsHide).
set SOURCE=%~dp0
set DIR=%SOURCE:~0,-1%
set PREFIX=%DIR%\..
set PATH=%DIR%;%PREFIX%\git\cmd;%PREFIX%\git\mingw64\bin;%PATH%
set PYTHONPATH=%PREFIX%\source;%PREFIX%\lib;%PYTHONPATH%
set PYTHONDONTWRITEBYTECODE=1
REM R90: python.exe 复制品在 bin/，DLL 在 python-dist/ → loader 找不到 → 用 python-dist 里的 exe（与 DLL 同目录）
REM 无窗口 daemon：GUI spawn 此脚本时 python.exe(console 子系统)会新开控制台黑窗；
REM 改优先用 pythonw.exe(GUI 子系统，不开窗口)。日志写 ~/.emrg/emrgd.log
REM (RotatingFileHandler)，StreamHandler 仅附加，无控制台不影响日志。
set PYEXE=%DIR%\python-dist\pythonw.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python3.13.exe
REM R124: `emrgd.cmd stop` — 优雅关闭 daemon（rant 2026-08-10T08:50:44 安装器预停止）：
REM 复用 CLI `emrg server stop`（协议 shutdown → ping-pid SIGTERM 兜底，见
REM emrg/__main__.py _stop_daemon）。pythonw 无控制台，print 丢弃无害，shutdown 无需 stdout。
REM 用标签而非括号块，保证 %errorlevel% 在 python 退出后才展开。
if /I not "%~1"=="stop" goto :start
"%PYEXE%" -m emrg server stop
exit /b %errorlevel%
:start
"%PYEXE%" -m emrg.server %*
