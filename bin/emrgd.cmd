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
set PYEXE=%DIR%\python-dist\python.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python3.13.exe
"%PYEXE%" -m emrg.server %*
