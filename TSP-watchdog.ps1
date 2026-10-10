# =====================================================================
#  TSP watchdog  --  "启动窗口一消失, 服务就停" 的兜底保证
#
#  由 TSP-run.ps1 以**独立(隐藏)窗口**启动, 因此它不会随启动窗口一起被杀:
#  它只等一件事 —— 启动窗口进程(守护进程)退出, 然后按记录把服务进程树清干净。
#
#  为什么需要它:
#    关闭控制台窗口时, Windows 只会终止"附属于该控制台"的进程; 而 pnpm ->
#    node/vite -> esbuild 这类多级 spawn 有可能留下孤儿(实测进程链里就有
#    中间 cmd)。只靠控制台语义不够稳, 所以这里按 PID 显式 /T 清树。
#
#  只做一件事, 不做判断: 读状态文件 -> taskkill /T /F -> 删状态文件。
# =====================================================================
param(
  [Parameter(Mandatory = $true)][int]$SupervisorPid,
  [Parameter(Mandatory = $true)][string]$StatePath
)

$ErrorActionPreference = 'SilentlyContinue'

function Kill-Tree([int]$ProcessId) {
  if ($ProcessId -le 0) { return }
  $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
  if ($p) { & taskkill /PID $ProcessId /T /F 2>&1 | Out-Null }
}

# 1) 等启动窗口退出（正常退出 / Ctrl+C / 关窗被系统终止 / 被强杀 —— 都会走到这里）
try { Wait-Process -Id $SupervisorPid -ErrorAction SilentlyContinue } catch { }
# 双保险: 万一 Wait-Process 立刻返回(进程本就不存在)也稍等一下, 避免与正常收尾抢
Start-Sleep -Seconds 2

# 2) 按状态文件清理服务进程树
$state = $null
try { $state = Get-Content -Path $StatePath -Raw -ErrorAction Stop | ConvertFrom-Json } catch { }

if ($state) {
  foreach ($pid0 in @($state.backend_pid, $state.frontend_pid)) {
    Kill-Tree ([int]$pid0)
  }
}

# 3) 兜底: 状态文件缺失/不全时, 按特征清理本项目自己的进程（绝不动别的程序）
$root = ''
if ($state) { $root = [string]$state.root }
if (-not $root) { $root = Split-Path -Parent $PSCommandPath }

Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'uvicorn app\.main:app' -and $_.CommandLine -match [regex]::Escape($root) } |
  ForEach-Object { Kill-Tree $_.ProcessId }

Get-CimInstance Win32_Process -Filter "Name='node.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match [regex]::Escape($root) } |
  ForEach-Object { Kill-Tree $_.ProcessId }

Get-CimInstance Win32_Process -Filter "Name='cmd.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'uvicorn app\.main:app' -and $_.CommandLine -match [regex]::Escape($root) } |
  ForEach-Object { Kill-Tree $_.ProcessId }

# 4) 清状态文件（收尾幂等: 正常路径也删过, 删不到就算了）
Remove-Item -Path $StatePath -Force -ErrorAction SilentlyContinue
