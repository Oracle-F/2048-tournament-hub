[CmdletBinding()]
param(
    [string]$Title = "NapCat 登录状态提醒",
    [Parameter(Mandatory = $true)]
    [string]$Message,
    [string]$StatePath = "",
    [string]$RuntimeLogPath = ""
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms

$form = New-Object System.Windows.Forms.Form
$form.TopMost = $true
$form.StartPosition = "CenterScreen"
$form.ShowInTaskbar = $false
$form.Opacity = 0
$form.Show()
$form.Activate()

$details = @()
if ($StatePath) {
    $details += "状态文件: $StatePath"
}
if ($RuntimeLogPath) {
    $details += "日志文件: $RuntimeLogPath"
}

$fullMessage = $Message
if ($details.Count -gt 0) {
    $fullMessage = ($Message, "", ($details -join "`r`n")) -join "`r`n"
}

[System.Windows.Forms.MessageBox]::Show(
    $form,
    $fullMessage,
    $Title,
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Warning
) | Out-Null

$form.Close()
