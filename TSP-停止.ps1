# TSP portable stop script (called by TSP-停止.bat)
#
# Stops ONLY this project's services. ASCII-only on purpose:
# Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI, so Chinese text
# here would be garbled. Chinese guidance lives in the .bat and the docs.
#
# Matching rules (the backend is launched with a RELATIVE --app-dir, so its
# own command line carries no absolute path):
#   1) process EXECUTABLE lives under the project folder
#      (backend\.venv\Scripts\python.exe, frontend\node_modules\...\node.exe)
#   2) its command line mentions the project folder
#      (vite runs from a global node.exe -> only rule 2 catches it)
#   3) python.exe running "uvicorn app.main:app"       (TSP backend)
#   4) cmd.exe wrapper window that runs that backend
#
# Safety rails (learned the hard way):
#   - never kill this script's own process chain (the shell that launched it)
#   - never kill anything that is not a service-like process
#   - kill each hit together with its children (/T)

$ErrorActionPreference = 'SilentlyContinue'

$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
$Root = $Root.TrimEnd('\')
$rl = $Root.ToLower()

$all = Get-CimInstance Win32_Process
$byParent = @{}
$byName = @{}
foreach ($x in $all) {
    $byParent[[int]$x.ProcessId] = [int]$x.ParentProcessId
    $byName[[int]$x.ProcessId] = $x.Name
}

# ancestors of this script (cmd wrapper -> shell -> ...): must never be killed
$chain = New-Object System.Collections.Generic.List[int]
$cursor = $PID
while ($byParent.ContainsKey($cursor) -and -not $chain.Contains($cursor)) {
    $chain.Add($cursor)
    $cursor = $byParent[$cursor]
}

$serviceLike = @('node.exe', 'python.exe', 'pythonw.exe', 'cmd.exe', 'esbuild.exe', 'pnpm.exe')

$hits = @()
foreach ($p in $all) {
    $pid_ = [int]$p.ProcessId
    if ($chain.Contains($pid_)) { continue }
    if ($serviceLike -notcontains $p.Name) { continue }
    $cmd = [string]$p.CommandLine
    $cmdLower = $cmd.ToLower()
    $exe = [string]$p.ExecutablePath

    $m1 = $exe -and $exe.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)
    $m2 = $cmd -and $cmdLower.Contains($rl)
    $m3 = $p.Name -eq 'python.exe' -and $cmd -match 'uvicorn\s+app\.main:app'
    $m4 = $p.Name -eq 'cmd.exe' -and $cmd -match 'uvicorn\s+app\.main:app'
    if ($m1 -or $m2 -or $m3 -or $m4) { $hits += $p }
}

# close the wrapper windows too: add a cmd.exe parent of any hit (never an ancestor)
foreach ($h in @($hits)) {
    $pp = [int]$h.ParentProcessId
    if ($pp -and $byName.ContainsKey($pp) -and $byName[$pp] -eq 'cmd.exe' -and -not $chain.Contains($pp)) {
        $parentProc = $all | Where-Object { [int]$_.ProcessId -eq $pp }
        if ($parentProc) { $hits += $parentProc }
    }
}

$hits = $hits | Sort-Object ProcessId -Unique

if (-not $hits) {
    Write-Host '  (no TSP service process found)'
} else {
    foreach ($h in $hits) {
        Write-Host ("  stopping PID {0}  {1}" -f $h.ProcessId, $h.Name)
        & taskkill.exe /PID $h.ProcessId /T /F 2>&1 | Out-Null
    }
}

Write-Host ''
Write-Host '--- port check ---'
foreach ($port in @(3018, 3011)) {
    $busy = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host ("  :{0} STILL in use (pid {1})" -f $port, $busy[0].OwningProcess)
    } else {
        Write-Host ("  :{0} free" -f $port)
    }
}
