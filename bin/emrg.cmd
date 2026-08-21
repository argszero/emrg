@echo off
REM EMRG launcher -- installed at %USERPROFILE%\.emrg\install\bin\emrg.cmd
REM Phase 4 installer (rant #12) section 2:
REM   - R60: Windows git lives in install\git\ -- PATH needs git\cmd + git\mingw64\bin.
REM   - R35: %~dp0 under a .lnk shortcut resolves to the .cmd's real location.
REM   - R13: PYTHONPATH includes source\ (parent of the emrg package).
REM   - R61: source\ is read-only -- PYTHONDONTWRITEBYTECODE (zero-write acceptance).
set SOURCE=%~dp0
set DIR=%SOURCE:~0,-1%
set PREFIX=%DIR%\..
set PATH=%DIR%;%PREFIX%\git\cmd;%PREFIX%\git\mingw64\bin;%PATH%
set PYTHONPATH=%PREFIX%\source;%PREFIX%\lib;%PYTHONPATH%
set PYTHONDONTWRITEBYTECODE=1
REM R90: python.exe copy lives in bin/, DLLs in python-dist/ -> loader would miss them -> use the exe in python-dist (same dir as DLLs)
set PYEXE=%DIR%\python-dist\python.exe
if not exist "%PYEXE%" set PYEXE=%DIR%\python-dist\python3.13.exe
"%PYEXE%" -m emrg %*
