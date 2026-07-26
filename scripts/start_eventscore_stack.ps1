[CmdletBinding()]
param(
    [int]$NapCatDelaySeconds = 45,
    [int]$BotPort = 8080
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$botLauncher = Join-Path $root "run_bot.bat"
$napcatLaunchScript = Join-Path $root "scripts\run_napcat_with_log.ps1"
$statePath = Join-Path $root "data\tmp\bot_connection_state.json"
$napcatRuntimeLog = Join-Path $root "data\tmp\napcat_runtime.log"
$startupLog = Join-Path $root "data\tmp\eventscore_startup.log"

function Write-StartupLogLine {
    param([string]$Line)
    try {
        $logDir = Split-Path -Parent $startupLog
        if ($logDir) {
            New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        }
        Add-Content -LiteralPath $startupLog -Value ("{0} {1}" -f (Get-Date -Format o), $Line)
    } catch {
        # Startup must not fail just because logging is unavailable.
    }
}

function Test-PortListening {
    param([int]$Port)
    $matches = netstat -ano | Select-String (":{0}\s+.*LISTENING" -f $Port)
    return $null -ne $matches
}

function Resolve-NapCatBatPath {
    $candidatePaths = @(
        "C:\napcat\NapCat.44498.Shell\napcat.bat",
        "C:\napcat\bootmain\napcat.bat"
    )

    foreach ($candidate in $candidatePaths) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    return $null
}

function Read-BotConnectionState {
    if (-not (Test-Path -LiteralPath $statePath)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Test-NapCatProcessRunning {
    return $null -ne (Get-Process -Name "NapCatWinBootMain" -ErrorAction SilentlyContinue | Select-Object -First 1)
}

Write-StartupLogLine "startup begin root=$root"

if (-not (Test-Path -LiteralPath $botLauncher)) {
    Write-StartupLogLine "bot launcher missing: $botLauncher"
    throw "Bot launcher not found: $botLauncher"
}

if (Test-PortListening -Port $BotPort) {
    Write-StartupLogLine "bot already listening on port $BotPort"
} else {
    Write-StartupLogLine "starting bot via $botLauncher"
    Start-Process -FilePath $botLauncher -WorkingDirectory $root -WindowStyle Hidden | Out-Null
}

if ($NapCatDelaySeconds -gt 0) {
    Start-Sleep -Seconds $NapCatDelaySeconds
}

if (-not (Test-Path -LiteralPath $napcatLaunchScript)) {
    Write-StartupLogLine "NapCat launch script missing: $napcatLaunchScript"
    throw "NapCat launch script not found: $napcatLaunchScript"
} else {
    $state = Read-BotConnectionState
    if (Test-NapCatProcessRunning) {
        Write-StartupLogLine "NapCat process already running; skipping launch."
    } elseif ($state -and $state.status -eq "online") {
        Write-StartupLogLine "NapCat already online; skipping launch."
    } else {
        $napcatBatPath = Resolve-NapCatBatPath
        if (-not $napcatBatPath) {
            Write-StartupLogLine "NapCat batch file missing: C:\napcat\NapCat.44498.Shell\napcat.bat or C:\napcat\bootmain\napcat.bat"
            throw "NapCat batch file not found. Check the NapCat install path."
        }

        Write-StartupLogLine "starting NapCat directly via $napcatLaunchScript"
        & $napcatLaunchScript -NapCatBatPath $napcatBatPath -LogPath $napcatRuntimeLog -ConsoleLike -HideLauncherWindow
    }
}

if (Test-Path -LiteralPath $statePath) {
    try {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        Write-StartupLogLine ("state status={0} self_id={1}" -f $state.status, $state.self_id)
    } catch {
        Write-StartupLogLine "state read failed: $($_.Exception.Message)"
    }
}

Write-StartupLogLine "startup end"
