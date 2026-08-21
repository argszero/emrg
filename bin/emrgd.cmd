@echo off
REM EMRG daemon launcher -- installed at %USERPROFILE%\.emrg\install\bin\emrgd.cmd
REM Phase 4 installer (rant #12) section 2 -- same structure as bin\emrg.cmd; entry is
REM the server package. GUI main.js spawns this path (R36 shell:true, R66 windowsHide).
set SOURCE=%~dp0
set DIR=%SOURCE:~0,-1%
set PREFIX=%DIR%\..
set PATH=%DIR%;%PREFIX%\git\cmd;%PREFIX%\git\mingw64\bin;%PATH%
set PYTHONPATH=%PREFIX%\source;%PREFIX%\lib;%PYTHONPATH%
set PYTHONDONTWRITEBYTECODE=1
REM R90: python.exe copy lives in bin/, DLLs in python-dist/ -> loader would miss them -> use the exe in python-dist (same dir as DLLs)
REM Windowless daemon: when the GUI spawns this script, python.exe (console subsystem) would open a black console window;
REM prefer pythonw.exe (GUI subsystem, no window). Logs go to ~/.emrg/emrgd.log
REM (RotatingFileHandler); StreamHandler only attaches, no console so logging is unaffected.
set PYEXE=%DIR%\python-dist\pythonw.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python3.13.exe
REM R124: `emrgd.cmd stop` - graceful daemon shutdown (rant 2026-08-10T08:50:44 installer pre-stop):
REM reuses the CLI `emrg server stop` (protocol shutdown -> ping-pid SIGTERM fallback, see
REM emrg/__main__.py _stop_daemon). pythonw has no console; dropped prints are harmless, shutdown needs no stdout.
REM uses labels not parenthesized blocks so %errorlevel% expands only after python exits.
if /I not "%~1"=="stop" goto :start
"%PYEXE%" -m emrg server stop
exit /b %errorlevel%
:start
"%PYEXE%" -m emrg.server %*
