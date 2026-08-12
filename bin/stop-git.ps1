# stop-git.ps1 -- force-kill every process under the bundled git install prefix
# (host 2026-08-11T19:47:44 FINAL decision, OVERRIDES #689: install success has
#  priority -- "要杀死相关进程，如果杀死git时，发现sh/vim导致杀不掉，则一起杀。"
#  If sh/vim hold msys-2.0.dll and block the git-tree kill, they are killed too.)
#
# 1. collect all processes whose ExecutablePath is under
#    %USERPROFILE%\.emrg\install\git\ (the portable Git the daemon spawns for
#    evolution-cycle ops; system Git under Program Files is NEVER matched)
# 2. pass 1: force-kill the git tree (git.exe/ssh.exe/plink.exe/bash.exe)
# 3. pass 2: guilt-by-association -- force-kill EVERY survivor under the prefix
#    (sh/vim included, per host decision; 0.2.25 msys-2.0.dll popup must not regress)
# 4. exit 0 when nothing survives (or nothing was running -- clean install safe);
#    exit 1 when a process still holds the prefix (installer aborts with a hint)
#
# Run via `powershell -NoProfile -ExecutionPolicy Bypass -File stop-git.ps1`
# (a standalone script -- no cmd inline-PowerShell quoting escapes, the root
#  cause of the 0.2.26 step-4 escape bug).

$ErrorActionPreference = 'SilentlyContinue'
$prefix = "$env:USERPROFILE\.emrg\install\git\*"

# pass 1: git tree (portable Git executables under the prefix)
Get-CimInstance Win32_Process |
  Where-Object { $_.ExecutablePath -like $prefix -and $_.Name -in @('git.exe', 'ssh.exe', 'plink.exe', 'bash.exe') } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

# pass 2: guilt-by-association -- anything still under the prefix (sh/vim/...)
Get-CimInstance Win32_Process |
  Where-Object { $_.ExecutablePath -like $prefix } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300

$left = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like $prefix })
if ($left.Count -gt 0) {
  # Truthful failure (rant 2026-08-12T12:30:41): never report success while a
  # process still holds the prefix. Name the survivors so the installer can
  # show a useful message instead of a silent bogus exit=0.
  $left | ForEach-Object { Write-Host ("still running: {0} (pid {1})" -f $_.Name, $_.ProcessId) }
  exit 1
}
exit 0
