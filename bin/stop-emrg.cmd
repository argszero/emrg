@echo off
REM stop-emrg.cmd -- gracefully stop EMRG GUI/TUI/daemon before the installer
REM overwrites ~\.emrg\install files (rant 2026-08-10T08:50:44: Inno Setup got
REM stuck at "stopping existing processes" because the windowless pythonw daemon holds file locks
REM and Inno CloseApplications cannot see it).
REM
REM Order matters (mirrors bin/emrg-uninstall steps 1a/1b):
REM   1. GUI   (EMRG.exe): graceful WM_CLOSE first (taskkill without /F), then
REM      UNCONDITIONAL /F after the ~5s grace window (host 01:27:07Z: long-lived
REM      GUI deferred WM_CLOSE past 5s -- /F must not be gated on a survivor check)
REM   2. TUI   (python.exe -m emrg): command-line filter (wmic, PowerShell
REM      fallback), excludes the daemon (pythonw.exe -m emrg.server)
REM   3. daemon: `emrg server stop` protocol shutdown via the OLD install's CLI
REM      (present since #364 -- version-safe), emrgd.pid poll (<=10s), then
REM      taskkill /F /PID fallback
REM   4. bundled git (rant 2026-08-11T17:56:25): kill orphaned git/ssh/bash
REM      processes whose executable lives under %INSTALL%\git\ (the portable Git
REM      the daemon spawns for evolution-cycle git ops). When the daemon is killed
REM      mid-operation these become orphans holding install\git\usr\bin\msys-2.0.dll
REM      -> Inno "DeleteFile failed; code 5" on upgrade. Filter is by executable
REM      PATH so system Git (C:\Program Files\Git) is never touched.
REM
REM Returns 0 when nothing EMRG-related survives; 1 if a process could not be
REM stopped (installer aborts with a clear message instead of hanging).
REM Safe on clean install: no old install dir -> everything is skipped -> 0.
setlocal enabledelayedexpansion
set "EMRG_DIR=%USERPROFILE%\.emrg"
set "INSTALL=%EMRG_DIR%\install"

REM --- 1. GUI: graceful WM_CLOSE, then unconditional /F after the grace window ---
REM    (host report 2026-08-10T01:27:07Z: a long-lived ~15h GUI session deferred
REM    WM_CLOSE past the 5s window -- two full script runs did not terminate it
REM    while a direct `taskkill /IM EMRG.exe` did. => the /F fallback must be
REM    UNCONDITIONAL after the wait so an old GUI session can never hold the
REM    installer hostage. When the GUI is not running, the graceful taskkill
REM    returns errorlevel 1 (skip the wait) and the /F below is a fast no-op.)
taskkill /IM EMRG.exe >nul 2>&1
if not errorlevel 1 (
  REM give the GUI up to ~5s to exit cleanly (ping = portable sleep)
  ping -n 6 127.0.0.1 >nul 2>&1
)
taskkill /F /IM EMRG.exe >nul 2>&1

REM --- 2. TUI: python.exe -m emrg (daemon is pythonw.exe -m emrg.server, excluded) ---
where wmic >nul 2>&1
if not errorlevel 1 (
  REM %% = literal % in batch files (wmic LIKE wildcard)
  wmic process where "name='python.exe' and commandline like '%%-m emrg%%' and commandline not like '%%emrg.server%%'" call terminate >nul 2>&1
) else (
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '-m emrg' -and $_.CommandLine -notmatch 'emrg\.server' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
)

REM --- 3. daemon: protocol shutdown via old install's CLI, pid poll, /F fallback ---
if exist "%INSTALL%\bin\emrg.cmd" (
  call "%INSTALL%\bin\emrg.cmd" server stop
)
if not exist "%EMRG_DIR%\emrgd.pid" goto :verify
set /a TRIES=0
:wait_pid
if not exist "%EMRG_DIR%\emrgd.pid" goto :verify
set /a TRIES+=1
if %TRIES% geq 10 goto :kill_pid
ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_pid
:kill_pid
set "DPID="
for /f "usebackq delims=" %%p in ("%EMRG_DIR%\emrgd.pid") do set "DPID=%%p"
if defined DPID taskkill /F /PID %DPID% >nul 2>&1

REM --- 4. bundled git: orphaned git/ssh/bash under %INSTALL%\git (msys-2.0.dll lock) ---
REM    (host 2026-08-11T17:56:25: daemon killed mid-git-op leaves orphans holding
REM    install\git\usr\bin\msys-2.0.dll -> Inno DeleteFile code 5. Filter by
REM    ExecutablePath prefix so system Git is never touched. `\"` = PowerShell
REM    quote escape precedent: TUI branch line 44.)
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

:verify
set "EXIT_CODE=0"
tasklist /FI "IMAGENAME eq EMRG.exe" 2>nul | findstr /i "EMRG.exe" >nul && set "EXIT_CODE=1"
REM %-variables inside a parenthesized block expand at parse time (after set "DPID=" they hold the old/empty value) -> must use
REM enabledelayedexpansion !DPID! (runtime expansion); if defined itself is a runtime check.
if exist "%EMRG_DIR%\emrgd.pid" (
  set "DPID="
  for /f "usebackq delims=" %%p in ("%EMRG_DIR%\emrgd.pid") do set "DPID=%%p"
  if defined DPID (
    tasklist /FI "PID eq !DPID!" 2>nul | findstr /i "!DPID!" >nul && set "EXIT_CODE=1"
  )
)
REM bundled-git survival check: any process still under install\git\ -> exit 1
REM (installer aborts with a clear message instead of a repeat "Try again" loop)
powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" }) { exit 1 }" >nul 2>&1
if errorlevel 1 set "EXIT_CODE=1"
endlocal & exit /b %EXIT_CODE%
