@echo off
chcp 65001 >nul
REM stop-emrg.cmd -- fixed v2: no nested-parenthesis %VAR% expansion bugs
REM (v1 failed: "10 was unexpected" + "no emrg.cmd" -- %VAR% inside ( ) blocks
REM  expands at parse time. Use labels + !VAR! delayed expansion instead.)
setlocal enabledelayedexpansion
set "EMRG_DIR=%USERPROFILE%\.emrg"
set "INSTALL=%EMRG_DIR%\install"
set "EXIT_CODE=0"
echo [stop-emrg] ============ begin ============
echo [stop-emrg] EMRG_DIR=%EMRG_DIR%
echo [stop-emrg] INSTALL=%INSTALL%

echo [1] check GUI (EMRG.exe)...
taskkill /IM EMRG.exe >nul 2>&1
if not errorlevel 1 (
  echo [1] graceful found, wait 5s...
  ping -n 6 127.0.0.1 >nul 2>&1
)
echo [1] force-kill GUI (taskkill /F)...
taskkill /F /IM EMRG.exe >nul 2>&1
echo [1] GUI done (exit=%errorlevel%)

echo [2] check TUI (python -m emrg)...
where wmic >nul 2>&1
if not errorlevel 1 goto :tui_wmic
echo [2] kill TUI via PowerShell...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match '-m emrg' -and $_.CommandLine -notmatch 'emrg\.server' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
echo [2] PowerShell done (exit=%errorlevel%)
goto :tui_done
:tui_wmic
echo [2] kill TUI via wmic...
wmic process where "name='python.exe' and commandline like '%%-m emrg%%' and commandline not like '%%emrg.server%%'" call terminate >nul 2>&1
echo [2] wmic done (exit=%errorlevel%)
:tui_done

echo [3] check daemon...
if exist "%INSTALL%\bin\emrg.cmd" goto :daemon_stop
echo [3] no emrg.cmd at %INSTALL%\bin -- skip protocol stop
goto :daemon_pid
:daemon_stop
echo [3] call emrg server stop...
call "%INSTALL%\bin\emrg.cmd" server stop
echo [3] emrg server stop done (exit=%errorlevel%)
:daemon_pid
if exist "%EMRG_DIR%\emrgd.pid" goto :daemon_pid_wait
echo [3] no emrgd.pid (daemon not running) -- skip, continue to step 4
goto :step4
:daemon_pid_wait
set /a TRIES=0
:wait_pid
if not exist "%EMRG_DIR%\emrgd.pid" goto :pid_gone
set /a TRIES+=1
if !TRIES! geq 10 goto :kill_pid
ping -n 2 127.0.0.1 >nul 2>&1
goto :wait_pid
:kill_pid
set "DPID="
for /f "usebackq delims=" %%p in ("%EMRG_DIR%\emrgd.pid") do set "DPID=%%p"
if defined DPID (
  echo [3] force-kill daemon PID !DPID!...
  taskkill /F /PID !DPID! >nul 2>&1
  echo [3] daemon kill done (exit=%errorlevel%)
)
:pid_gone
echo [3] daemon pid gone

:step4
echo [4] check+kill bundled git (install\git)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='SilentlyContinue'; $prefix=\"$env:USERPROFILE\.emrg\install\git\*\"; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix -and $_.Name -in @('git.exe','ssh.exe','plink.exe','bash.exe') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Milliseconds 300; $left = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix }); if ($left.Count -gt 0) { $left | ForEach-Object { Write-Host (\"still running: {0} (pid {1})\" -f $_.Name, $_.ProcessId) }; exit 1 }; exit 0"
echo [4] git kill done (exit=%errorlevel%)
if errorlevel 1 set "EXIT_CODE=1"

:verify
echo [verify] check residual GUI...
tasklist /FI "IMAGENAME eq EMRG.exe" 2>nul | findstr /i "EMRG.exe" >nul && set "EXIT_CODE=1"
echo [verify] GUI residual (EXIT_CODE=%EXIT_CODE%)
if exist "%EMRG_DIR%\emrgd.pid" (
  set "DPID="
  for /f "usebackq delims=" %%p in ("%EMRG_DIR%\emrgd.pid") do set "DPID=%%p"
  if defined DPID (
    tasklist /FI "PID eq !DPID!" 2>nul | findstr /i "!DPID!" >nul && set "EXIT_CODE=1"
  )
)
echo [verify] check residual git...
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like \"$env:USERPROFILE\.emrg\install\git\*\" }; if ($p) { $p | ForEach-Object { Write-Host (\"  [verify] residual: {0} (pid {1})\" -f $_.Name, $_.ProcessId) }; exit 1 }"
echo [verify] git residual (exit=%errorlevel%)
if errorlevel 1 set "EXIT_CODE=1"

echo [stop-emrg] ============ end (EXIT_CODE=%EXIT_CODE%) ============
endlocal & exit /b %EXIT_CODE%
