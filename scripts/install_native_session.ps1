<# Install a worker-local lifetime owner outside Deadline's render process tree.
Warm mode is permitted only when the Worker/GPU is reserved for ComfyUI. #>
[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [Parameter(Mandatory=$true)][string]$Worker,
    [Parameter(Mandatory=$true)][string]$GpuUuid,
    [Parameter(Mandatory=$true)][string]$ComfyRoot,
    [string]$StateRoot = "$env:LOCALAPPDATA\ComfyUI-Deadline\native-session",
    [int]$BrokerPort = 8202,
    [int]$ComfyPort = 8198,
    [int]$IdleSeconds = 60,
    [ValidateSet('none','auto','latent2rgb','taesd')][string]$PreviewMethod = 'auto',
    [switch]$ExclusiveWorker,
    [string]$BorrowedUrl = "",
    [string]$BorrowedInputRoot = "",
    [string]$BorrowedOutputRoot = "",
    [string]$BorrowedTempRoot = "",
    [string[]]$CustomNodes = @()
)
$ErrorActionPreference = 'Stop'
if (!$ExclusiveWorker -and !$BorrowedUrl) { throw 'Persistent warm owners require -ExclusiveWorker on a ComfyUI-only GPU. Normal render tasks need no installed owner.' }
if ($Worker -notmatch '^[A-Za-z0-9_.-]+$' -or $GpuUuid -notmatch '^GPU-[a-fA-F0-9-]+$') { throw 'Invalid Worker/GPU identity' }
if ($BrokerPort -lt 1024 -or $BrokerPort -gt 65535 -or $ComfyPort -lt 1024 -or $ComfyPort -gt 65535) { throw 'Invalid port' }
if ($IdleSeconds -lt 1 -or $IdleSeconds -gt 3600) { throw 'Idle timeout must be 1 to 3600 seconds' }
$resolvedComfy = (Resolve-Path -LiteralPath $ComfyRoot).Path
if (!(Test-Path -LiteralPath (Join-Path $resolvedComfy 'main.py'))) { throw 'ComfyRoot must contain main.py' }
$python = Join-Path (Split-Path -Parent $resolvedComfy) 'python_embeded\python.exe'
if (!(Test-Path -LiteralPath $python)) { throw 'Portable Python was not found' }
$project = Split-Path -Parent $PSScriptRoot
$owner = Join-Path $project 'farm_session_owner.py'
$taskName = "ComfyUI-NativeQueue-$Worker-$BrokerPort"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and $existing.State -eq 'Running') { throw "Drain/stop the existing owner before updating $taskName" }
# Quotes are literal argument separators for Windows process creation.
foreach ($value in @($resolvedComfy,$StateRoot,$BorrowedUrl,$BorrowedInputRoot,$BorrowedOutputRoot,$BorrowedTempRoot) + $CustomNodes) {
    if ($value.Contains('"') -or $value.Contains("`n") -or $value.Contains("`r")) { throw 'Invalid quoted argument' }
}
$arguments = '-s "' + $owner + '" --worker "' + $Worker + '" --gpu-uuid "' + $GpuUuid + '" --comfy-root "' + $resolvedComfy + '" --state-root "' + $StateRoot + '" --broker-port ' + $BrokerPort + ' --comfy-port ' + $ComfyPort + ' --idle-seconds ' + $IdleSeconds
$arguments += ' --preview-method ' + $PreviewMethod
if ($ExclusiveWorker) { $arguments += ' --exclusive-worker' }
if ($BorrowedUrl) {
    if (!$BorrowedInputRoot -or !$BorrowedOutputRoot) { throw 'Borrowed sessions require their actual input and output folders' }
    $arguments += ' --borrowed-url "' + $BorrowedUrl + '" --borrowed-input-root "' + $BorrowedInputRoot + '" --borrowed-output-root "' + $BorrowedOutputRoot + '"'
    if ($BorrowedTempRoot) { $arguments += ' --borrowed-temp-root "' + $BorrowedTempRoot + '"' }
}
if ($CustomNodes.Count) { $arguments += ' --custom-nodes ' + (($CustomNodes | ForEach-Object { '"' + $_ + '"' }) -join ' ') }
if ($PSCmdlet.ShouldProcess($taskName, 'Register and start native session owner')) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $resolvedComfy
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $principal.UserId
    $settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "Installed $taskName. Configure NativeWorkerSessions in Deadline's ComfyUI plugin."
    if (!$ExclusiveWorker -and !$BorrowedUrl) { Write-Host 'Shared Worker mode: GPU/process is released before each task completes.' }
}
