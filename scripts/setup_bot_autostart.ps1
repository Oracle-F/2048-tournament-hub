[CmdletBinding()]
param(
    [int]$DelaySeconds = 45
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$taskName = "EventScore-QQBot-Autostart"

$python = "C:\Users\oracl\AppData\Local\Programs\Python\Python312\python.exe"

$scriptPath = Join-Path $root "scripts\run_private_qq_bot.py"
if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Bot script not found: $scriptPath"
}

$action = New-ScheduledTaskAction -Execute $python -Argument "`"$scriptPath`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
if ($DelaySeconds -gt 0) {
    $trigger.Delay = "PT${DelaySeconds}S"
}
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "Auto start QQ private bot for event score hub" -Force | Out-Null
    Write-Host "Created/updated startup task: $taskName"
    if ($DelaySeconds -gt 0) {
        Write-Host "Startup delay: $DelaySeconds seconds"
    } else {
        Write-Host "Startup delay: none"
    }
    Write-Host "Check it in Task Scheduler Library."
} catch {
    Write-Error "Failed to create scheduled task: $($_.Exception.Message)"
    throw
}
