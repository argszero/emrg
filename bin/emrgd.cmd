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
"%DIR%\python.exe" -m emrg.server %*
