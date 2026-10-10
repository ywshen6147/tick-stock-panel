# =====================================================================
#  TSP foreground supervisor  (called by TSP-启动.bat)
#
#  Why this exists
#  ---------------
#  The backend (:3018) and frontend (:3011) are started as CHILDREN OF THE
#  CURRENT CONSOLE (Start-Process -NoNewWindow). Windows terminates every
#  process attached to a console when that window is closed, so:
#
#      >>> closing this window stops TSP completely <<<
#
#  ...with no need to run TSP-停止.bat. The explicit taskkill in the finally
#  block is a second safety net for the Ctrl+C / "one service died" paths.
#
#  Deliberately NO --reload on uvicorn: a reloader re-execs a new worker and
#  the two can race for data-dir locks right at startup.
#
#  ASCII-only output on purpose: the parent console runs the OEM codepage
#  (936 on zh-CN), so non-ASCII text here would come out as mojibake.
# =====================================================================
param(
  [Parameter(Mandatory = $true)][string]$Root,
  [string]$Uv = 'uv',
  [string]$Pnpm = 'pnpm'
)

$ErrorActionPreference = 'Continue'

$backendDir = Join-Path $Root 'backend'
$frontendDir = Join-Path $Root 'frontend'
$dataDir = Join-Path $Root 'data'
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir -Force | Out-Null }
$py = Join-Path $backendDir '.venv\Scripts\python.exe'

function Info([string]$m) { Write-Host "[TSP] $m" -ForegroundColor Cyan }

if (-not (Test-Path $py)) {
  Write-Host "[X] backend venv not found: $py" -ForegroundColor Red
  exit 1
}

$started = @()
$statePath = Join-Path $dataDir '.tsp_run.json'

function Write-State {
  # 状态文件: 供看门狗在启动窗口消失后按 PID 精准清理（/T 连带子树）
  # 注意: 值先算好再放进 hashtable —— Windows PowerShell 5.1 不接受在
  # hashtable 字面量里直接写 if 表达式（会把后面的 } 判成意外的标记）。
  try {
    $beId = 0
    $feId = 0
    if ($started.Count -ge 1 -and $started[0]) { $beId = [int]$started[0].Id }
    if ($started.Count -ge 2 -and $started[1]) { $feId = [int]$started[1].Id }
    $obj = [ordered]@{
      supervisor_pid = $PID
      backend_pid    = $beId
      frontend_pid   = $feId
      root           = $Root
      started_at     = (Get-Date).ToString('s')
    }
    $obj | ConvertTo-Json | Set-Content -Path $statePath -Encoding UTF8
  } catch { }
}

function Start-Watchdog {
  # 独立(隐藏)窗口启动 —— 这样它不会随启动窗口一起被杀, 才能在关窗后收尾。
  try {
    Start-Process -FilePath 'powershell.exe' `
      -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
                      '-File', (Join-Path $Root 'TSP-watchdog.ps1'),
                      '-SupervisorPid', $PID, '-StatePath', $statePath) `
      -WindowStyle Hidden | Out-Null
    Info 'watchdog armed (stops the services when this window disappears)'
  } catch {
    Write-Host "[!] watchdog could not start: $($_.Exception.Message)" -ForegroundColor Yellow
  }
}

function Start-Backend {
  Info 'starting backend  :3018 ...'
  $p = Start-Process -FilePath $py `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', '.',
                    '--host', '0.0.0.0', '--port', '3018') `
    -WorkingDirectory $backendDir -NoNewWindow -PassThru `
    -RedirectStandardOutput (Join-Path $dataDir 'backend.log') `
    -RedirectStandardError (Join-Path $dataDir 'backend.log.err')
  $script:started += $p
  return $p
}

function Start-Frontend {
  Info 'starting frontend :3011 ...'
  $cmdline = "$Pnpm dev --host 0.0.0.0 --port 3011"
  $p = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', $cmdline) `
    -WorkingDirectory $frontendDir -NoNewWindow -PassThru `
    -RedirectStandardOutput (Join-Path $dataDir 'frontend.log') `
    -RedirectStandardError (Join-Path $dataDir 'frontend.log.err')
  $script:started += $p
  return $p
}

function Wait-Port([int]$Port, [int]$TimeoutSec) {
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    $conn = $null
    try { $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue } catch { }
    if (-not $conn) {
      try { $conn = netstat -an | Select-String -Pattern ":$Port\s.*LISTENING" } catch { }
    }
    if ($conn) { return $true }
    Start-Sleep -Milliseconds 700
  }
  return $false
}

function Stop-All {
  foreach ($p in $started) {
    if ($p -and -not $p.HasExited) {
      try { & taskkill /PID $p.Id /T /F 2>&1 | Out-Null } catch { }
    }
  }
  Remove-Item -Path $statePath -Force -ErrorAction SilentlyContinue
}

$be = $null
$fe = $null
try {
  $be = Start-Backend
  $beOk = Wait-Port -Port 3018 -TimeoutSec 120
  if ($beOk) { Info 'backend  ready: http://127.0.0.1:3018' }
  else { Write-Host '[!] backend did not open :3018 within 120s - check data\backend.log.err' -ForegroundColor Yellow }

  $fe = Start-Frontend
  # 立刻武装看门狗: 即使守护进程在启动阶段就死掉(关窗/崩溃), 服务也会被清理
  Write-State
  Start-Watchdog

  $feOk = Wait-Port -Port 3011 -TimeoutSec 180
  if ($feOk) {
    Info 'frontend ready: http://127.0.0.1:3011'
    Start-Process 'http://127.0.0.1:3011/' | Out-Null
  } else {
    Write-Host '[!] frontend did not open :3011 within 180s - check data\frontend.log.err' -ForegroundColor Yellow
  }

  Write-Host ''
  Write-Host '=============================================================' -ForegroundColor Yellow
  Write-Host '  TSP is RUNNING.' -ForegroundColor Yellow
  Write-Host '  >>> CLOSE THIS WINDOW to stop TSP (backend + frontend) <<<' -ForegroundColor Yellow
  Write-Host '  (open in browser: http://127.0.0.1:3011/)' -ForegroundColor Yellow
  Write-Host '=============================================================' -ForegroundColor Yellow
  Write-Host ''

  # 守护: 1) 关窗 -> Windows 终止本控制台进程; 2) 看门狗发现本进程消失 -> 清服务树
  #       3) 任一服务自己退出 -> 这里收尾(下面循环)
  while ($true) {
    if ($be.HasExited) { Info "backend exited (code $($be.ExitCode)) - stopping the rest"; break }
    if ($fe.HasExited) { Info "frontend exited (code $($fe.ExitCode)) - stopping the rest"; break }
    Start-Sleep -Seconds 2
  }
} finally {
  Info 'stopping TSP ...'
  Stop-All
  Info 'TSP stopped.'
}
