<#
.SYNOPSIS
Deploys this repository's Deadline render plugin to the configured Deadline Repository.

.DESCRIPTION
Finds the Deadline Repository path via deadlinecommand -GetRepositoryPath, then copies
plugins\ComfyUI into <Repository>\custom\plugins\ComfyUI.

You can also pass -RepositoryPath explicitly. The value may be either the repository
root or the custom plugins directory.

.EXAMPLE
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1

.EXAMPLE
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1 -RepositoryPath "\\YOUR-SERVER\Repository"

.EXAMPLE
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1 -RepositoryPath "\\YOUR-SERVER\Repository\custom\plugins"
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$RepositoryPath = "",
    [string]$DeadlineCommand = "",
    [string]$PluginName = "ComfyUI",
    [switch]$Clean,
    [switch]$NoBackup
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[deploy-deadline-plugin] $Message"
}

function Resolve-DeadlineCommand {
    param([string]$ExplicitPath)

    $candidates = @()
    if ($ExplicitPath) {
        $candidates += $ExplicitPath
    }
    if ($env:DEADLINE_PATH) {
        $candidates += (Join-Path $env:DEADLINE_PATH "deadlinecommand.exe")
        $candidates += (Join-Path $env:DEADLINE_PATH "deadlinecommand")
    }
    $candidates += "C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe"
    $candidates += "/opt/Thinkbox/Deadline10/bin/deadlinecommand"

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Could not find deadlinecommand. Pass -DeadlineCommand or set DEADLINE_PATH."
}

function Get-DeadlineRepositoryPath {
    param(
        [string]$ExplicitRepositoryPath,
        [string]$DeadlineCommandPath
    )

    if ($ExplicitRepositoryPath) {
        return $ExplicitRepositoryPath.Trim().Trim('"')
    }

    foreach ($argument in @("-GetRepositoryPath", "-GetRepositoryRoot")) {
        $output = & $DeadlineCommandPath $argument 2>&1
        if ($LASTEXITCODE -eq 0) {
            $path = (@($output) | ForEach-Object { "$_" } | Where-Object { ![string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1)
            if ($path) {
                $path = $path.ToString().Trim()
            }
            if ($path -and $path -notmatch "Bad submission arguments") {
                return $path
            }
        }
    }

    throw "deadlinecommand did not return a repository path."
}

function Resolve-CustomPluginsPath {
    param([string]$RepositoryOrPluginsPath)

    $path = $RepositoryOrPluginsPath.Trim().Trim('"')
    if ([string]::IsNullOrWhiteSpace($path)) {
        throw "Repository path is empty."
    }

    $leaf = Split-Path -Leaf $path
    $parent = Split-Path -Parent $path
    $parentLeaf = if ($parent) { Split-Path -Leaf $parent } else { "" }

    if ($leaf -ieq "plugins" -and $parentLeaf -ieq "custom") {
        return $path
    }

    return (Join-Path $path "custom\plugins")
}

function Copy-PluginDirectory {
    param(
        [string]$SourceDirectory,
        [string]$DestinationDirectory,
        [bool]$ShouldClean
    )

    if (!(Test-Path -LiteralPath $SourceDirectory)) {
        throw "Source plugin directory does not exist: $SourceDirectory"
    }

    $destinationParent = Split-Path -Parent $DestinationDirectory
    if (!(Test-Path -LiteralPath $destinationParent)) {
        if ($PSCmdlet.ShouldProcess($destinationParent, "Create custom plugins directory")) {
            New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        }
    }

    if ($ShouldClean -and (Test-Path -LiteralPath $DestinationDirectory)) {
        $resolvedDestination = (Resolve-Path -LiteralPath $DestinationDirectory).Path
        if ($resolvedDestination -match "\\custom\\plugins\\[^\\]+$") {
            if ($PSCmdlet.ShouldProcess($resolvedDestination, "Remove existing plugin directory before deploy")) {
                Remove-Item -LiteralPath $resolvedDestination -Recurse -Force
            }
        }
        else {
            throw "Refusing to clean unexpected destination path: $resolvedDestination"
        }
    }

    if (!(Test-Path -LiteralPath $DestinationDirectory)) {
        if ($PSCmdlet.ShouldProcess($DestinationDirectory, "Create plugin directory")) {
            New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
        }
    }

    if ($PSCmdlet.ShouldProcess($DestinationDirectory, "Copy Deadline plugin files from $SourceDirectory")) {
        robocopy $SourceDirectory $DestinationDirectory /E /XF "*.pyc" /XD "__pycache__" /R:3 /W:2 /NFL /NDL /NP
        $exitCode = $LASTEXITCODE
        if ($exitCode -gt 7) {
            throw "robocopy failed with exit code $exitCode"
        }
    }
}

$deadlineCommandPath = Resolve-DeadlineCommand -ExplicitPath $DeadlineCommand
$repositoryPathResolved = Get-DeadlineRepositoryPath -ExplicitRepositoryPath $RepositoryPath -DeadlineCommandPath $deadlineCommandPath
$customPluginsPath = Resolve-CustomPluginsPath -RepositoryOrPluginsPath $repositoryPathResolved

$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDirectory
$sourcePluginDirectory = Join-Path $projectRoot "plugins\$PluginName"
$destinationPluginDirectory = Join-Path $customPluginsPath $PluginName

Write-Step "deadlinecommand: $deadlineCommandPath"
Write-Step "Repository/custom plugins path: $customPluginsPath"
Write-Step "Source: $sourcePluginDirectory"
Write-Step "Destination: $destinationPluginDirectory"

if (!(Test-Path -LiteralPath $customPluginsPath)) {
    throw "Custom plugins path does not exist or is not reachable: $customPluginsPath"
}

if (!$NoBackup -and (Test-Path -LiteralPath $destinationPluginDirectory)) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupDirectory = Join-Path $customPluginsPath "$PluginName.backup_$timestamp"
    if ($PSCmdlet.ShouldProcess($backupDirectory, "Create backup of current $PluginName Deadline plugin")) {
        Copy-Item -LiteralPath $destinationPluginDirectory -Destination $backupDirectory -Recurse -Force
        Write-Step "Backup created: $backupDirectory"
    }
}

Copy-PluginDirectory -SourceDirectory $sourcePluginDirectory -DestinationDirectory $destinationPluginDirectory -ShouldClean ([bool]$Clean)

Write-Step "Deploy complete."
Write-Step "If workers have cached the old plugin, restart them or submit a new job so Deadline copies the updated plugin sandbox."
