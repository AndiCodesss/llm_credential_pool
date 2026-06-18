$ErrorActionPreference = 'SilentlyContinue'

$taskName = 'CLIProxyAPI'
$port = 8317
$home = Join-Path $env:USERPROFILE '.cli-proxy-api'
$log = Join-Path $home 'watchdog.log'
$stopFile = Join-Path $home 'STOP'

function Log($message) {
  $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  Add-Content -Path $log -Value "[$timestamp] $message"
}

if (Test-Path $stopFile) {
  Log "STOP file present; watchdog not starting task."
  exit 0
}

$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if (-not $task) {
  Log "Task $taskName not found."
  exit 1
}

$taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
$listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue

if ($task.State -ne 'Running' -or -not $listening) {
  Log "Unhealthy: task_state=$($task.State), listening=$([bool]$listening), last_result=$($taskInfo.LastTaskResult). Restarting $taskName."
  Start-ScheduledTask -TaskName $taskName | Out-Null
  Start-Sleep -Seconds 10
  $listeningAfter = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  $taskAfter = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  Log "After restart: task_state=$($taskAfter.State), listening=$([bool]$listeningAfter)."
} else {
  Log "Healthy: task_state=$($task.State), listening=true."
}
