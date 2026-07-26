[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NapCatTaskName,

    [string]$WatchdogTaskName = "EventScore-NapCat-Watchdog",
    [string]$PopupScriptPath = "",

    [int]$IntervalMinutes = 1,

    [int]$DelaySeconds = 75
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$watchdogScript = Join-Path $root "scripts\watch_bot_connection.py"
$defaultPopupScript = Join-Path $root "scripts\show_bot_connection_alert.ps1"

function Resolve-PythonExecutablePath {
    $venvPython = Join-Path $root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python -and $python.Source) {
        return $python.Source
    }

    $python = Get-Command "python" -ErrorAction SilentlyContinue
    if ($python -and $python.Source) {
        return $python.Source
    }

    throw "Python executable not found. Install Python or create .venv\\Scripts\\python.exe first."
}

function Resolve-PythonwExecutablePath {
    $venvPythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
    if (Test-Path -LiteralPath $venvPythonw) {
        return $venvPythonw
    }

    $pythonw = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue
    if ($pythonw -and $pythonw.Source) {
        return $pythonw.Source
    }

    $pythonw = Get-Command "pythonw" -ErrorAction SilentlyContinue
    if ($pythonw -and $pythonw.Source) {
        return $pythonw.Source
    }

    return $null
}

function Resolve-LauncherScriptPath {
    $documentsDir = [Environment]::GetFolderPath("MyDocuments")
    if ($documentsDir) {
        $codexDir = Join-Path $documentsDir "Codex"
        if (Test-Path -LiteralPath $codexDir) {
            return Join-Path $codexDir "eventscore_napcat_watchdog.ps1"
        }
    }
    return Join-Path $root "eventscore_napcat_watchdog.ps1"
}

$launcherScriptPath = Resolve-LauncherScriptPath
$python = Resolve-PythonExecutablePath
$pythonw = Resolve-PythonwExecutablePath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $launcherScriptPath) | Out-Null

if (-not (Test-Path -LiteralPath $watchdogScript)) {
    throw "Watchdog script not found: $watchdogScript"
}

$popupScript = $PopupScriptPath
if (-not $popupScript) {
    $popupScript = $defaultPopupScript
}

if ($DelaySeconds -lt 0) {
    $DelaySeconds = 0
}
if ($IntervalMinutes -lt 1) {
    $IntervalMinutes = 1
}
$delayMinutes = [math]::Floor($DelaySeconds / 60)
$delaySecondsRemainder = $DelaySeconds % 60
$delayValue = "{0:0000}:{1:00}" -f $delayMinutes, $delaySecondsRemainder
$launcherContent = @"
$env:BOT_CONNECTION_WATCHDOG_ENABLE_POPUP = "1"
& '$python' '$watchdogScript' --task-name '$NapCatTaskName' --popup-script '$popupScript'
"@
Set-Content -LiteralPath $launcherScriptPath -Value $launcherContent -Encoding UTF8

if ($pythonw) {
    $watchdogCommand = "`"$pythonw`" `"$watchdogScript`" --task-name `"$NapCatTaskName`" --popup-script `"$popupScript`""
    Write-Host "Watchdog runtime: pythonw.exe"
} else {
    $powershellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $watchdogCommand = "`"$powershellExe`" -NoProfile -NonInteractive -NoLogo -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherScriptPath`""
    Write-Host "Watchdog runtime: PowerShell launcher fallback"
}
$schtasks = "C:\Windows\System32\schtasks.exe"

try {
    $arguments = @(
        "/Create"
        "/TN", $WatchdogTaskName
        "/TR", $watchdogCommand
        "/SC", "MINUTE"
        "/MO", $IntervalMinutes.ToString()
        "/ST", "00:00"
        "/DELAY", $delayValue
        "/F"
    )
    $result = Start-Process -FilePath $schtasks -ArgumentList $arguments -WorkingDirectory $root -PassThru -Wait -WindowStyle Hidden
    if ($result.ExitCode -ne 0) {
        throw "schtasks exit code $($result.ExitCode)"
    }
    Write-Host "Created/updated watchdog task: $WatchdogTaskName"
    Write-Host "Watching NapCat task: $NapCatTaskName"
    Write-Host "Popup script: $popupScript"
    Write-Host "Popup mode: enabled"
    Write-Host "Launcher script: $launcherScriptPath"
    Write-Host "Interval: $IntervalMinutes minute(s)"
    if ($DelaySeconds -gt 0) {
        Write-Host "Startup delay: $DelaySeconds seconds"
    }
} catch {
    Write-Error "Failed to create watchdog task: $($_.Exception.Message)"
    throw
}
