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
REM   4. bundled git: kill ONLY EMRG-owned git/ssh/bash subprocess trees under
REM      %INSTALL%\git\ (portable Git the daemon spawns for evolution-cycle ops).
REM      Rant 2026-08-11T17:56:25: orphans hold msys-2.0.dll -> Inno code 5.
REM      Rant 2026-08-11T18:56:58: blanket path kill also hit the HOST's Git Bash
REM      sh/vim -> only EMRG trees (ancestor = daemon/TUI) are killed; host tools
REM      are never touched (same class as R125).
REM
REM Returns 0 when nothing EMRG-related survives; 1 if a process could not be
REM stopped (installer aborts with a clear message instead of hanging).
REM Safe on clean install: no old install dir -> everything is skipped -> 0.
setlocal enabledelayedexpansion
set "EMRG_DIR=%USERPROFILE%\.emrg"
set "INSTALL=%EMRG_DIR%\install"
REM PID snapshot file: EMRG-owned process tree captured BEFORE any kill, so dead
REM parents are irrelevant (review 2026-08-11T19:03: ancestor walk cannot resolve
REM dead daemon parents -- the #683 primary orphan case regresses).
set "PIDFILE=%TEMP%\emrg-stop-pids.txt"
del /q "%PIDFILE%" >nul 2>&1

REM --- 0. snapshot EMRG-owned tree (daemon + TUI + GUI + descendants) ---
REM    Walk DOWN from the roots (emrgd.pid, EMRG.exe, python.exe -m emrg) while they
REM    are still alive, collecting the full descendant PID set. Step 4 / :verify use
REM    ONLY this recorded set -> host Git Bash sh/vim (not descendants of EMRG roots)
REM    are never touched (rant 2026-08-11T18:56:58), and orphans of an already-dead
REM    daemon are still captured (their PID was recorded while the daemon lived).
powershell -NoProfile -Command "$f=Get-CimInstance Win32_Process; $roots=New-Object System.Collections.Generic.List[int]; if(Test-Path \"%EMRG_DIR%\emrgd.pid\"){ $d=Get-Content \"%EMRG_DIR%\emrgd.pid\" -ErrorAction SilentlyContinue; if($d -match '^\d+$'){ $roots.Add([int]$d) } }; $f | Where-Object { $_.Name -eq 'EMRG.exe' -or ($_.Name -eq 'python.exe' -and $_.CommandLine -match '-m emrg' -and $_.CommandLine -notmatch 'emrg\.server') } | ForEach-Object { $roots.Add([int]$_.ProcessId) }; $set=New-Object 'System.Collections.Generic.HashSet[int]'; foreach($r in $roots){ [void]$set.Add($r) }; $changed=$true; while($changed){ $changed=$false; foreach($p in $f){ if($set.Contains([int]$p.ParentProcessId) -and -not $set.Contains([int]$p.ProcessId)){ [void]$set.Add([int]$p.ProcessId); $changed=$true } } }; $set | ForEach-Object { $_ } | Set-Content -Path \"%PIDFILE%\" -Encoding ascii" >nul 2>&1

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

REM --- 4. bundled git: kill only RECORDED EMRG-owned git/ssh/bash (snapshot step 0) ---
REM    (rant 2026-08-11T17:56:25: daemon killed mid-git-op leaves orphans holding
REM    install\git\usr\bin\msys-2.0.dll -> Inno DeleteFile code 5.)
REM    (host 2026-08-11T18:56:58: blanket path kill also caught the HOST's Git Bash
REM    sh/vim -- vim edits lost + installer aborted. Same class as R125.) Only PIDs
REM    recorded in step 0 (EMRG tree captured while daemon/TUI were alive) are killed
REM    when their executable is under install\git\. Host Git Bash sh/vim are not in
REM    the recorded set -> never touched.
if exist "%PIDFILE%" (
  powershell -NoProfile -Command "$f=Get-CimInstance Win32_Process; $ids=@(Get-Content \"%PIDFILE%\" | ForEach-Object { [int]$_ }); $f | Where-Object { $ids -contains [int]$_.ProcessId -and $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
)

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
REM EMRG-owned bundled-git survival check: only RECORDED EMRG git PIDs still alive
REM -> exit 1 (installer aborts with a restart hint). Host Git Bash sh/vim alive is
REM NOT a failure (rant 2026-08-11T18:56:58).
if exist "%PIDFILE%" (
  powershell -NoProfile -Command "$f=Get-CimInstance Win32_Process; $ids=@(Get-Content \"%PIDFILE%\" | ForEach-Object { [int]$_ }); $surv = $f | Where-Object { $ids -contains [int]$_.ProcessId -and $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" }; if ($surv) { exit 1 }" >nul 2>&1
  if errorlevel 1 set "EXIT_CODE=1"
)
del /q "%PIDFILE%" >nul 2>&1
endlocal & exit /b %EXIT_CODE%
