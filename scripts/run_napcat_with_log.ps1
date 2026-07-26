[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NapCatBatPath,
    [string]$LogPath = "",
    [string]$WorkingDirectory = "",
    [switch]$NoBanner,
    [switch]$HideLauncherWindow,
    [switch]$ConsoleLike,
    [switch]$ForceLaunch
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if (-not $LogPath) {
    $LogPath = Join-Path $root "data\tmp\napcat_runtime.log"
}

if (-not (Test-Path -LiteralPath $NapCatBatPath)) {
    throw "NapCat batch file not found: $NapCatBatPath"
}

if (-not $WorkingDirectory) {
    $WorkingDirectory = Split-Path -Parent $NapCatBatPath
}

$logDirectory = Split-Path -Parent $LogPath
if ($logDirectory) {
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
}

function Write-LauncherLogLine {
    param([string]$Line)
    try {
        Add-Content -LiteralPath $LogPath -Value $Line
    } catch {
        if (-not $NoBanner) {
            Write-Host ("NapCat log write skipped: {0}" -f $_.Exception.Message)
        }
    }
}

function Get-RunningNapCatProcesses {
    return @(Get-Process -Name "NapCatWinBootMain" -ErrorAction SilentlyContinue)
}

function Resolve-WtExecutablePath {
    $wt = Get-Command "wt.exe" -ErrorAction SilentlyContinue
    if ($wt -and $wt.Source) {
        return $wt.Source
    }

    $localAppData = $env:LOCALAPPDATA
    if ($localAppData) {
        $candidate = Join-Path $localAppData "Microsoft\WindowsApps\wt.exe"
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    return $null
}

function New-NapCatTeeCommand {
    $escapedBatPath = $NapCatBatPath.Replace("'", "''")
    $escapedLogPath = $LogPath.Replace("'", "''")
    return "& '$escapedBatPath' 2>&1 | Tee-Object -FilePath '$escapedLogPath' -Append"
}

function New-NapCatEncodedTeeCommand {
    $teeCommand = New-NapCatTeeCommand
    return [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($teeCommand))
}

if (-not $NoBanner) {
    Write-LauncherLogLine "===== NapCat launch $(Get-Date -Format o) ====="
    Write-LauncherLogLine ("working_directory={0}" -f $WorkingDirectory)
    Write-LauncherLogLine ("bat_path={0}" -f $NapCatBatPath)
}

if (-not $ForceLaunch) {
    $runningNapCatProcesses = Get-RunningNapCatProcesses
    if ($runningNapCatProcesses.Count -gt 0) {
        $runningProcessIds = ($runningNapCatProcesses | ForEach-Object { $_.Id }) -join ","
        Write-LauncherLogLine ("===== NapCat launch skipped: process already running pid={0} at {1} =====" -f $runningProcessIds, (Get-Date -Format o))
        exit 0
    }
}

try {
    $windowStyle = if ($HideLauncherWindow) { "Hidden" } else { "Normal" }
    if ($ConsoleLike) {
        $powershellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $encodedTeeCommand = New-NapCatEncodedTeeCommand
        $wtPath = Resolve-WtExecutablePath
        if ($wtPath) {
            $arguments = @(
                "new-tab"
                "-d"
                $WorkingDirectory
                $powershellExe
                "-NoLogo"
                "-NoExit"
                "-ExecutionPolicy"
                "Bypass"
                "-EncodedCommand"
                $encodedTeeCommand
            )
            Write-LauncherLogLine ("terminal_path={0}" -f $wtPath)
            Write-LauncherLogLine "console_output=tee_to_runtime_log"
            Start-Process -FilePath $wtPath -ArgumentList $arguments -WorkingDirectory $WorkingDirectory -WindowStyle $windowStyle | Out-Null
            if (-not $NoBanner) {
                Write-LauncherLogLine ("===== NapCat launched in Windows Terminal at {0} =====" -f (Get-Date -Format o))
            }
            exit 0
        }
        Write-LauncherLogLine "terminal_path=cmd.exe (wt.exe not found)"
        Write-LauncherLogLine "console_output=tee_to_runtime_log"
        $process = Start-Process -FilePath $powershellExe -ArgumentList "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encodedTeeCommand -WorkingDirectory $WorkingDirectory -PassThru -Wait -WindowStyle $windowStyle
    } else {
        $command = 'call "{0}" >> "{1}" 2>&1' -f $NapCatBatPath, $LogPath
        $process = Start-Process -FilePath "cmd.exe" -ArgumentList "/d", "/c", $command -WorkingDirectory $WorkingDirectory -PassThru -Wait -WindowStyle $windowStyle
    }
    if (-not $NoBanner) {
        Write-LauncherLogLine ("===== NapCat exit code {0} at {1} =====" -f $process.ExitCode, (Get-Date -Format o))
    }
    exit $process.ExitCode
} catch {
    Write-LauncherLogLine ("===== NapCat launch failed at {0}: {1} =====" -f (Get-Date -Format o), $_.Exception.Message)
    throw
}
