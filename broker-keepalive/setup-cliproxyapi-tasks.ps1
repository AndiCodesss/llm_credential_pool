$ErrorActionPreference = 'Stop'

$homeDir = Join-Path $env:USERPROFILE '.cli-proxy-api'
$hiddenLauncher = Join-Path $homeDir 'start-broker-hidden.vbs'
$watchdogVbs = Join-Path $homeDir 'watchdog-cliproxyapi.vbs'

if (-not (Test-Path $hiddenLauncher)) { throw "Missing hidden launcher: $hiddenLauncher" }
if (-not (Test-Path $watchdogVbs)) { throw "Missing watchdog: $watchdogVbs" }

# Visible terminal popups came from running cmd.exe/powershell.exe directly as interactive scheduled tasks.
# Use wscript.exe + VBScript launchers so all supervision runs hidden.
$brokerAction = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$hiddenLauncher`""
$brokerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$brokerSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -MultipleInstances IgnoreNew `
  -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'CLIProxyAPI' -Action $brokerAction -Trigger $brokerTrigger -Settings $brokerSettings -Force | Out-Null

$watchdogAction = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$watchdogVbs`""
$watchdogTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$watchdogSettings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'CLIProxyAPI-Watchdog' -Action $watchdogAction -Trigger $watchdogTrigger -Settings $watchdogSettings -Force | Out-Null

Write-Host "Configured hidden CLIProxyAPI task: wscript.exe `"$hiddenLauncher`""
Write-Host "Configured hidden watchdog task: wscript.exe `"$watchdogVbs`" every 5 minutes"
