# Registers a hidden scheduled task that runs the quota dashboard at logon and
# keeps it alive. Run once:  powershell -ExecutionPolicy Bypass -File setup-autostart.ps1
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$vbs  = Join-Path $here 'start-dashboard-hidden.vbs'
if (-not (Test-Path $vbs)) { throw "missing $vbs" }

$action  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -MultipleInstances IgnoreNew `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'CLIProxyAPI-Dashboard' -Action $action `
  -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'CLIProxyAPI-Dashboard'
Write-Host "Registered + started 'CLIProxyAPI-Dashboard' (hidden, at logon). Open http://127.0.0.1:8788"
