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
REM   4. bundled git: INLINE guilt-by-association force-kill (single file,
REM      rant 2026-08-12T14:00:05 -- stop-git.ps1 merged into this script).
REM      Host 2026-08-11T19:47:44 FINAL decision OVERRIDES #689: install success
REM      has priority -- if sh/vim hold msys-2.0.dll and block the git-tree kill,
REM      they are killed too; only the install\git\ prefix is touched, system Git
REM      in Program Files is never affected. Step 4 runs PowerShell inline (same
REM      \" escaping as the TUI/verify snippets below, proven on v0.2.25-v0.2.27);
REM      every pass re-queries Get-CimInstance (no stale snapshot) and residual
REM      processes are reported truthfully via exit 1 (rant 2026-08-12T12:30:41).
REM
REM Returns 0 when nothing EMRG-related survives; 1 if a process could not be
REM stopped (installer aborts with a clear message instead of hanging).
REM Safe on clean install: no old install dir -> everything is skipped -> 0.
setlocal enabledelayedexpansion
set "EMRG_DIR=%USERPROFILE%\.emrg"
set "INSTALL=%EMRG_DIR%\install"
set "EXIT_CODE=0"

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

REM --- 4. bundled git: inline guilt-by-association force-kill (single file) ---
REM    (host 2026-08-11T19:47:44 FINAL decision OVERRIDES #689's snapshot-only
REM    approach: install success has priority -- if sh/vim hold msys-2.0.dll under
REM    install\git\ and block the git-tree kill, they are killed too; only the
REM    install\git\ prefix is touched, system Git (Program Files) is never matched.
REM    Rant 2026-08-12T14:00:05: stop-git.ps1 deleted, logic inlined below.
REM    Each pass re-queries Get-CimInstance (no stale snapshot); the survivor
REM    check uses the latest snapshot; residual -> exit 1 (truthful failure #701).
REM    The \" escaping matches the TUI/verify inline snippets proven on real
REM    Windows in v0.2.25-v0.2.27.)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $prefix=\"$env:USERPROFILE\.emrg\install\git\*\"; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix -and $_.Name -in @('git.exe','ssh.exe','plink.exe','bash.exe') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300; $left = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix }); if ($left.Count -gt 0) { $left | ForEach-Object { Write-Host (\"still running: {0} (pid {1})\" -f $_.Name, $_.ProcessId) }; exit 1 }; exit 0" >nul 2>&1
if errorlevel 1 set "EXIT_CODE=1"


:verify
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
REM Bundled-git survival check (plain prefix; guilt-by-association semantics):
REM any process still under install\git\ -> exit 1 (installer aborts). Same
REM condition as step 4's inline PowerShell; kept as belt-and-braces in case the
REM PowerShell invocation itself fails.
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" }; if ($p) { exit 1 }" >nul 2>&1
if errorlevel 1 set "EXIT_CODE=1"
endlocal & exit /b %EXIT_CODE%
