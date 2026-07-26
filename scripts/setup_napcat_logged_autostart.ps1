[CmdletBinding()]
param(
    [string]$NapCatBatPath = "",

    [string]$TaskName = "EventScore-NapCat-Autostart",

    [string]$LogPath = "",

    [int]$DelaySeconds = 45,

    [switch]$HideLauncherWindow
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$launchScript = Join-Path $root "scripts\run_napcat_with_log.ps1"
$statePath = Join-Path $root "data\tmp\bot_connection_state.json"

function Resolve-LauncherScriptPath {
    $documentsDir = [Environment]::GetFolderPath("MyDocuments")
    if ($documentsDir) {
        $codexDir = Join-Path $documentsDir "Codex"
        if (Test-Path -LiteralPath $codexDir) {
            return Join-Path $codexDir "eventscore_napcat_launch.ps1"
        }
    }
    return Join-Path $root "eventscore_napcat_launch.ps1"
}

$launcherScriptPath = Resolve-LauncherScriptPath
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $launcherScriptPath) | Out-Null

if (-not (Test-Path -LiteralPath $launchScript)) {
    throw "NapCat launch script not found: $launchScript"
}

if (-not $NapCatBatPath) {
    $candidatePaths = @(
        "C:\napcat\NapCat.44498.Shell\napcat.bat",
        "C:\napcat\bootmain\napcat.bat"
    )
    foreach ($candidate in $candidatePaths) {
        if (Test-Path -LiteralPath $candidate) {
            $NapCatBatPath = $candidate
            break
        }
    }
}

if (-not $NapCatBatPath) {
    throw "NapCat batch file not found. Pass -NapCatBatPath explicitly."
}

if (-not $LogPath) {
    $LogPath = Join-Path $root "data\tmp\napcat_runtime.log"
}

$launcherSwitches = if ($HideLauncherWindow) { " -HideLauncherWindow" } else { " -ConsoleLike" }
$launcherContent = @'
$ErrorActionPreference = "Stop"
$statePath = "__STATE_PATH__"
if (Test-Path -LiteralPath $statePath) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if ($state -and $state.status -eq "online") {
            Write-Host "NapCat already online; skipping launch."
            exit 0
        }
    } catch {
        Write-Host "NapCat state file could not be read; continuing launch."
    }
}
& "__LAUNCH_SCRIPT__" -NapCatBatPath "__NAPCAT_BAT__" -LogPath "__LOG_PATH__"__LAUNCH_SWITCHES__
'@
$launcherContent = $launcherContent.Replace("__STATE_PATH__", $statePath)
$launcherContent = $launcherContent.Replace("__LAUNCH_SCRIPT__", $launchScript)
$launcherContent = $launcherContent.Replace("__NAPCAT_BAT__", $NapCatBatPath)
$launcherContent = $launcherContent.Replace("__LOG_PATH__", $LogPath)
$launcherContent = $launcherContent.Replace("__LAUNCH_SWITCHES__", $launcherSwitches)
Set-Content -LiteralPath $launcherScriptPath -Value $launcherContent -Encoding UTF8

if ($DelaySeconds -lt 0) {
    $DelaySeconds = 0
}
$delayMinutes = [math]::Floor($DelaySeconds / 60)
$delaySecondsRemainder = $DelaySeconds % 60
$delayValue = "{0:0000}:{1:00}" -f $delayMinutes, $delaySecondsRemainder
$powershellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$launchCommand = "`"$powershellExe`" -NoProfile -NonInteractive -NoLogo -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcherScriptPath`""
$schtasks = "C:\Windows\System32\schtasks.exe"

try {
    $arguments = @(
        "/Create"
        "/TN", $TaskName
        "/TR", $launchCommand
        "/SC", "ONLOGON"
        "/DELAY", $delayValue
        "/F"
    )
    $result = Start-Process -FilePath $schtasks -ArgumentList $arguments -WorkingDirectory $root -PassThru -Wait -WindowStyle Hidden
    if ($result.ExitCode -ne 0) {
        throw "schtasks exit code $($result.ExitCode)"
    }
    Write-Host "Created/updated NapCat launch task: $TaskName"
    Write-Host "NapCat bat: $NapCatBatPath"
    Write-Host "Runtime log: $LogPath"
    Write-Host "Launcher script: $launcherScriptPath"
    if ($DelaySeconds -gt 0) {
        Write-Host "Startup delay: $DelaySeconds seconds"
    }
} catch {
    Write-Error "Failed to create NapCat launch task: $($_.Exception.Message)"
    throw
}
