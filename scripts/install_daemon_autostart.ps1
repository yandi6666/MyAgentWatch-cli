param(
    [string]$TaskName = "MyAgentWatch CLI Daemon",
    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "start_daemon_hidden.ps1"
$cliDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" -CliDir `"$cliDir`""

$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments -WorkingDirectory $cliDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Start myagentwatch-cli daemon at user logon." `
    -Force | Out-Null

Write-Host "Installed autostart task: $TaskName"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started task now: $TaskName"
}
