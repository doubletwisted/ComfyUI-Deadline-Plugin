# Enhanced Copy Models Script - With Full Diagnostics
# Run this script via Deadline Monitor. Worker => Remote Control => Execute Command => 
# powershell.exe -ExecutionPolicy Bypass -File "X:\scripts\copy_models_to_local.ps1" -DestinationDrive "C"

param(
    [string]$DestinationDrive = "D",  # Change to C, E, etc.
    [switch]$DiagnosticMode = $true   # Set to $false for normal runs
)

$sourceDir = "X:\AI\models"
$destDir = "$DestinationDrive`:\AI\models"
$modelListFile = "X:\scripts\modellist.txt"
$logDir = "X:\scripts\copy_models_to_local_logs"
$computerName = $env:COMPUTERNAME

# Initialize logging
$script:logFile = $null

# Function to write both to console and flush output for Deadline
function Write-DeadlineLog {
    param($Message, $Type = "Info")
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    
    switch ($Type) {
        "Error" { 
            Write-Host $logMessage -ForegroundColor Red
            Write-Error $Message
        }
        "Warning" { 
            Write-Host $logMessage -ForegroundColor Yellow
            Write-Warning $Message
        }
        "Success" {
            Write-Host $logMessage -ForegroundColor Green
        }
        default { 
            Write-Host $logMessage
        }
    }
    
    # Flush output for Deadline to capture
    [Console]::Out.Flush()
    
    # Also log to file (will be set later)
    if ($script:logFile) {
        $logMessage | Out-File -FilePath $script:logFile -Append -Encoding UTF8
    }
}

# Minimal change: Calculate required delta space (only files that need copying)
function Get-RequiredSpace {
    param(
        [string[]]$FilePaths
    )
    
    $totalDelta = 0
    $missingFiles = @()
    $validFiles = @()
    $willCopy = @()

    Write-DeadlineLog "Analyzing $($FilePaths.Count) files in model list (delta-aware)..."
    
    foreach ($src in $FilePaths) {
        if (-not (Test-Path -LiteralPath $src)) {
            $missingFiles += $src
            continue
        }

        $validFiles += $src

        # Map to destination path using same root mapping as main script
        $relativePath = $src -replace [regex]::Escape("X:\AI\models\"), ""
        $dst = Join-Path $destDir $relativePath

        $needsCopy = $false
        if (-not (Test-Path -LiteralPath $dst)) {
            $needsCopy = $true
        } else {
            $sItem = Get-Item -LiteralPath $src
            $dItem = Get-Item -LiteralPath $dst
            if ($sItem.LastWriteTime -gt $dItem.LastWriteTime) {
                $needsCopy = $true
            } elseif ($sItem.Length -ne $dItem.Length) {
                $needsCopy = $true
            }
        }

        if ($needsCopy) {
            $size = (Get-Item -LiteralPath $src).Length
            $totalDelta += $size
            $willCopy += $src
        }
    }
    
    return @{
        TotalDeltaBytes = [int64]$totalDelta
        TotalDeltaGB = [math]::Round($totalDelta / 1GB, 2)
        MissingFiles = $missingFiles
        ValidFiles = $validFiles
        WillCopy = $willCopy
    }
}

# Function to check available disk space
function Get-AvailableSpace {
    param($DrivePath)
    
    try {
        $driveLetter = (Split-Path $DrivePath -Qualifier).Replace(":", "")
        $drive = Get-PSDrive -Name $driveLetter -ErrorAction Stop
        return @{
            FreeBytes = $drive.Free
            FreeGB = [math]::Round($drive.Free / 1GB, 2)
            TotalGB = [math]::Round(($drive.Used + $drive.Free) / 1GB, 2)
        }
    } catch {
        throw "Cannot access drive $driveLetter`: $($_.Exception.Message)"
    }
}

# Function to format file size for display
function Format-FileSize {
    param([long]$Bytes)
    
    if ($Bytes -ge 1GB) { return "{0:N2} GB" -f ($Bytes / 1GB) }
    elseif ($Bytes -ge 1MB) { return "{0:N2} MB" -f ($Bytes / 1MB) }
    else { return "{0:N0} KB" -f ($Bytes / 1KB) }
}

# Enhanced function to handle robocopy with diagnostics
function Invoke-RobustRobocopy {
    param(
        [string]$SourceDir,
        [string]$DestDir, 
        [string]$FileName,
        [string]$RelativePath
    )
    
    $robocopyArgs = @(
        $SourceDir,
        $DestDir,
        $FileName,
        "/R:10",          # Retry 10 times
        "/W:30",          # Wait 30 seconds between retries
        "/V",             # Verbose output
        "/TS",            # Include source time stamps
        "/FP",            # Include full path names
        "/NP",            # No progress indicator
        "/MT:1"           # Single-threaded for reliability
    )
    
    Write-DeadlineLog "Executing: robocopy `"$SourceDir`" `"$DestDir`" `"$FileName`" /R:10 /W:30 /V"
    
    $robocopyOutput = robocopy @robocopyArgs 2>&1
    $exitCode = $LASTEXITCODE
    
    $result = @{
        Success = $false
        ExitCode = $exitCode
        Output = $robocopyOutput
        Message = ""
    }
    
    if ($exitCode -le 7) {
        $result.Success = $true
        $result.Message = "Completed with exit code $exitCode (success)"
    } else {
        $result.Message = "Failed with exit code $exitCode"
        Write-DeadlineLog "Robocopy failed for $RelativePath (exit: $exitCode)" "Error"
        Write-DeadlineLog "Full Output:" "Error"
        $robocopyOutput | ForEach-Object { Write-DeadlineLog "  $_" "Error" }
    }
    
    return $result
}

# =============================================================================
# MAIN SCRIPT EXECUTION
# =============================================================================

Write-DeadlineLog "=== ENHANCED COPY MODELS SCRIPT STARTED ===" "Success"
Write-DeadlineLog "Script running on: $env:COMPUTERNAME"
Write-DeadlineLog "Source directory: $sourceDir"
Write-DeadlineLog "Destination directory: $destDir"
Write-DeadlineLog "Diagnostic Mode: $DiagnosticMode"

# Create log directory if it doesn't exist
if (-not (Test-Path $logDir)) {
    try {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        Write-DeadlineLog "Created log directory: $logDir"
    } catch {
        Write-DeadlineLog "ERROR: Cannot create log directory: $($_.Exception.Message)" "Error"
        exit 1
    }
}

# Check if modellist.txt exists
if (-not (Test-Path $modelListFile)) {
    $script:logFile = "$logDir\$computerName-FAIL.txt"
    "" | Out-File -FilePath $script:logFile -Encoding UTF8
    Write-DeadlineLog "ERROR: Model list file not found: $modelListFile" "Error"
    Write-DeadlineLog "Log saved to: $script:logFile"
    exit 1
}

# Read model list
try {
    $filePaths = Get-Content $modelListFile | Where-Object { $_.Trim() -ne "" }
    Write-DeadlineLog "Successfully loaded model list: $($filePaths.Count) entries"
} catch {
    $script:logFile = "$logDir\$computerName-FAIL.txt"
    "" | Out-File -FilePath $script:logFile -Encoding UTF8
    Write-DeadlineLog "ERROR: Cannot read model list file: $($_.Exception.Message)" "Error"
    exit 1
}

# =============================================================================
# STEP 1: PRE-FLIGHT CHECKS
# =============================================================================

Write-DeadlineLog "=== STEP 1: PERFORMING PRE-FLIGHT CHECKS ==="

try {
    $spaceInfo = Get-RequiredSpace -FilePaths $filePaths
    $availableSpace = Get-AvailableSpace -DrivePath $destDir
    
    Write-DeadlineLog "Valid files in list: $($spaceInfo.ValidFiles.Count)"
    Write-DeadlineLog "Total space required (delta): $($spaceInfo.TotalDeltaGB) GB"
    Write-DeadlineLog "Available disk space: $($availableSpace.FreeGB) GB"
    
    if ($spaceInfo.MissingFiles.Count -gt 0) {
        Write-DeadlineLog "WARNING: $($spaceInfo.MissingFiles.Count) files missing from source:" "Warning"
        foreach ($missingFile in $spaceInfo.MissingFiles) {
            Write-DeadlineLog "  MISSING: $missingFile" "Warning"
        }
    }
    
    # Check if we have enough space (with 15% buffer for safety), based on delta only
    $bufferMultiplier = 1.15
    $requiredWithBuffer = [int64]($spaceInfo.TotalDeltaBytes * $bufferMultiplier)
    $requiredWithBufferGB = [math]::Round($requiredWithBuffer / 1GB, 2)
    
    if ($availableSpace.FreeBytes -lt $requiredWithBuffer) {
        $script:logFile = "$logDir\$computerName-FAIL.txt"
        "" | Out-File -FilePath $script:logFile -Encoding UTF8
        Write-DeadlineLog "ERROR: Insufficient disk space for required delta!" "Error"
        Write-DeadlineLog "Required (with 15% buffer): $requiredWithBufferGB GB" "Error"
        Write-DeadlineLog "Available: $($availableSpace.FreeGB) GB" "Error"
        Write-DeadlineLog "Shortfall: $([math]::Round(($requiredWithBuffer - $availableSpace.FreeBytes) / 1GB, 2)) GB" "Error"
        Write-DeadlineLog "Log saved to: $script:logFile"
        exit 1
    }
    
    Write-DeadlineLog "Space check PASSED (delta buffer: $requiredWithBufferGB GB)" "Success"
    
    # Test network connectivity to source
    Write-DeadlineLog "Testing network connectivity to source..."
    if (-not (Test-Path $sourceDir)) {
        throw "Cannot access source directory: $sourceDir"
    }
    Write-DeadlineLog "Network connectivity test PASSED" "Success"
    
} catch {
    $script:logFile = "$logDir\$computerName-FAIL.txt"
    "" | Out-File -FilePath $script:logFile -Encoding UTF8
    Write-DeadlineLog "ERROR: Pre-flight check failed: $($_.Exception.Message)" "Error"
    exit 1
}

# =============================================================================
# STEP 2: SMART CLEANUP - Remove files not in list
# =============================================================================

Write-DeadlineLog "=== STEP 2: CLEANING UP EXTRA FILES ==="

$deletedCount = 0
$deletedSize = 0

if (Test-Path $destDir) {
    try {
        Write-DeadlineLog "Scanning existing files in destination..."
        $existingFiles = Get-ChildItem -Path $destDir -Recurse -File | ForEach-Object { $_.FullName }
        Write-DeadlineLog "Found $($existingFiles.Count) existing files in destination"
        
        # Convert modellist paths to destination paths for comparison
        $expectedFiles = $spaceInfo.ValidFiles | ForEach-Object {
            $relativePath = $_ -replace [regex]::Escape("X:\AI\models\"), ""
            Join-Path $destDir $relativePath
        }
        
        # Find files that exist in destination but are not in our expected list
        $filesToDelete = $existingFiles | Where-Object { $_ -notin $expectedFiles }
        
        if ($filesToDelete.Count -gt 0) {
            Write-DeadlineLog "Found $($filesToDelete.Count) files to remove (not in model list)"
            
            foreach ($file in $filesToDelete) {
                try {
                    $fileSize = (Get-Item $file -ErrorAction SilentlyContinue).Length
                    Remove-Item $file -Force -ErrorAction Stop
                    $relativePath = $file -replace [regex]::Escape($destDir), ""
                    Write-DeadlineLog "Removed: $relativePath ($(Format-FileSize $fileSize))"
                    $deletedCount++
                    $deletedSize += $fileSize
                } catch {
                    Write-DeadlineLog "Failed to remove: $file - $($_.Exception.Message)" "Warning"
                }
            }
            
            # Clean up empty directories
            Write-DeadlineLog "Cleaning up empty directories..."
            try {
                Get-ChildItem -Path $destDir -Recurse -Directory | 
                    Where-Object { (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue).Count -eq 0 } | 
                    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            } catch {
                Write-DeadlineLog "Warning: Some empty directories could not be removed" "Warning"
            }
            
            Write-DeadlineLog "Cleanup completed: $deletedCount files removed, $(Format-FileSize $deletedSize)) freed" "Success"
        } else {
            Write-DeadlineLog "No extra files found - destination is clean" "Success"
        }
        
    } catch {
        Write-DeadlineLog "WARNING: Cleanup encountered errors: $($_.Exception.Message)" "Warning"
    }
} else {
    Write-DeadlineLog "Destination directory does not exist - will be created during copy"
}

# =============================================================================
# STEP 3: COPYING/UPDATING MODEL FILES WITH DIAGNOSTICS
# =============================================================================

Write-DeadlineLog "=== STEP 3: COPYING/UPDATING MODEL FILES ==="

$processedCount = 0
$copiedCount = 0
$skippedCount = 0
$errorCount = 0
$warningCount = 0
$totalCopiedSize = 0

foreach ($filePath in $spaceInfo.ValidFiles) {
    $processedCount++
    
    try {
        # Calculate relative path and destination
        $relativePath = $filePath -replace [regex]::Escape("X:\AI\models\"), ""
        $destFilePath = Join-Path $destDir $relativePath
        $destFileDir = Split-Path $destFilePath -Parent
        
        # Create destination directory if it doesn't exist
        if (-not (Test-Path $destFileDir)) {
            New-Item -ItemType Directory -Path $destFileDir -Force | Out-Null
        }
        
        # Check if copy is needed
        $needsCopy = $false
        $copyReason = ""
        
        if (-not (Test-Path $destFilePath)) {
            $needsCopy = $true
            $copyReason = "missing"
        } else {
            $sourceTime = (Get-Item $filePath).LastWriteTime
            $destTime = (Get-Item $destFilePath).LastWriteTime
            $sourceSize = (Get-Item $filePath).Length
            $destSize = (Get-Item $destFilePath).Length
            
            if ($sourceTime -gt $destTime) {
                $needsCopy = $true
                $copyReason = "newer"
            } elseif ($sourceSize -ne $destSize) {
                $needsCopy = $true
                $copyReason = "different size"
            }
        }
        
        if ($needsCopy) {
            $sourceFileDir = Split-Path $filePath -Parent
            $fileName = Split-Path $filePath -Leaf
            $fileSize = (Get-Item $filePath).Length
            
            Write-DeadlineLog "Copying ($copyReason): $relativePath ($(Format-FileSize $fileSize))"
            
            # Use enhanced robocopy
            $copyResult = Invoke-RobustRobocopy -SourceDir $sourceFileDir -DestDir $destFileDir -FileName $fileName -RelativePath $relativePath
            
            if ($copyResult.Success) {
                Write-DeadlineLog "SUCCESS: $relativePath - $($copyResult.Message)" "Success"
                $copiedCount++
                $totalCopiedSize += $fileSize
                
                if ($copyResult.ExitCode -gt 0) {
                    Write-DeadlineLog "Note: $relativePath completed with warnings (exit code: $($copyResult.ExitCode))" "Warning"
                    $warningCount++
                }
            } else {
                Write-DeadlineLog "FAILED: $relativePath - $($copyResult.Message)" "Error"
                $errorCount++
                
                # Fallback to Copy-Item in diagnostic mode
                if ($DiagnosticMode) {
                    Write-DeadlineLog "Attempting fallback Copy-Item for $relativePath"
                    try {
                        Copy-Item -Path $filePath -Destination $destFilePath -Force -ErrorAction Stop
                        Write-DeadlineLog "Fallback SUCCESS: $relativePath copied via Copy-Item" "Success"
                        $copiedCount++
                        $totalCopiedSize += $fileSize
                    } catch {
                        Write-DeadlineLog "Fallback FAILED: $($_.Exception.Message)" "Error"
                    }
                }
            }
        } else {
            Write-DeadlineLog "Skipped (up to date): $relativePath"
            $skippedCount++
        }
        
    } catch {
        Write-DeadlineLog "Exception processing $filePath : $($_.Exception.Message)" "Error"
        $errorCount++
    }
    
    # Progress update every 5 files or at end
    if (($processedCount % 5) -eq 0 -or $processedCount -eq $spaceInfo.ValidFiles.Count) {
        $percentComplete = [math]::Round(($processedCount / $spaceInfo.ValidFiles.Count) * 100, 1)
        Write-DeadlineLog "Progress: $percentComplete% ($processedCount/$($spaceInfo.ValidFiles.Count)) | Copied: $copiedCount | Skipped: $skippedCount | Errors: $errorCount | Warnings: $warningCount"
    }
}

# =============================================================================
# FINAL STATUS AND LOGGING
# =============================================================================

Write-DeadlineLog "=== OPERATION COMPLETED ==="

$finalStatus = if ($errorCount -eq 0 -and $warningCount -eq 0) { 
    "SUCCESS" 
} elseif ($errorCount -eq 0 -and $warningCount -gt 0) { 
    "SUCCESS_WITH_WARNINGS" 
} elseif ($copiedCount -gt 0 -or $skippedCount -gt 0) { 
    "PARTIAL_SUCCESS" 
} else { 
    "FAILED" 
}

Write-DeadlineLog "Final Status: $finalStatus" $(if ($finalStatus -eq "SUCCESS") { "Success" } elseif ($finalStatus -eq "SUCCESS_WITH_WARNINGS") { "Warning" } elseif ($finalStatus -eq "PARTIAL_SUCCESS") { "Warning" } else { "Error" })
Write-DeadlineLog "Files in model list: $($filePaths.Count)"
Write-DeadlineLog "Files processed: $processedCount"
Write-DeadlineLog "Files copied: $copiedCount ($(Format-FileSize $totalCopiedSize))"
Write-DeadlineLog "Files skipped: $skippedCount"
Write-DeadlineLog "Files with warnings: $warningCount"
Write-DeadlineLog "Files with errors: $errorCount"
Write-DeadlineLog "Files deleted: $deletedCount ($(Format-FileSize $deletedSize))"

# Set final log file based on status
if ($finalStatus -eq "SUCCESS") {
    $script:logFile = "$logDir\$computerName-SUCCESS.txt"
    $scriptExitCode = 0
} elseif ($finalStatus -eq "SUCCESS_WITH_WARNINGS") {
    $script:logFile = "$logDir\$computerName-WARNING.txt"
    $scriptExitCode = 0
} elseif ($finalStatus -eq "PARTIAL_SUCCESS") {
    $script:logFile = "$logDir\$computerName-PARTIAL.txt"
    $scriptExitCode = 2
} else {
    $script:logFile = "$logDir\$computerName-FAIL.txt"
    $scriptExitCode = 1
}

# Create comprehensive summary log
$summaryLog = "=== ENHANCED COPY MODELS SCRIPT - EXECUTION SUMMARY ===`n"
$summaryLog += "Computer: $computerName`n"
$summaryLog += "Date/Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')`n"
$summaryLog += "Final Status: $finalStatus`n`n"

$summaryLog += "CONFIGURATION:`n"
$summaryLog += "  Source Directory: $sourceDir`n"
$summaryLog += "  Destination Directory: $destDir`n"
$summaryLog += "  Model List File: $modelListFile`n`n"

$summaryLog += "PRE-FLIGHT CHECKS:`n"
$summaryLog += "  Models in list: $($filePaths.Count)`n"
$summaryLog += "  Valid source files: $($spaceInfo.ValidFiles.Count)`n"
$summaryLog += "  Missing source files: $($spaceInfo.MissingFiles.Count)`n"
$summaryLog += "  Total space required (delta): $($spaceInfo.TotalDeltaGB) GB`n"
$summaryLog += "  Available disk space: $($availableSpace.FreeGB) GB`n`n"

$summaryLog += "CLEANUP RESULTS:`n"
$summaryLog += "  Extra files removed: $deletedCount`n"
$summaryLog += "  Space freed: $(Format-FileSize $deletedSize))`n`n"

$summaryLog += "COPY RESULTS:`n"
$summaryLog += "  Files processed: $processedCount`n"
$summaryLog += "  Files copied: $copiedCount ($(Format-FileSize $totalCopiedSize))`n"
$summaryLog += "  Files skipped (up-to-date): $skippedCount`n"
$summaryLog += "  Files with warnings: $warningCount`n"
$summaryLog += "  Files with errors: $errorCount`n`n"

$summaryLog += "EXIT CODE: $scriptExitCode`n`n"

$summaryLog += "OPERATION: Mirror-like sync of AI models based on modellist.txt`n"
$summaryLog += "  Only files in modellist.txt are preserved in destination`n"
$summaryLog += "  Extra files not in list are automatically removed`n"
$summaryLog += "  Files are only copied if missing, newer, or different size`n"
$summaryLog += "  Enhanced robocopy with diagnostics and fallback to Copy-Item"

$summaryLog | Out-File -FilePath $script:logFile -Encoding UTF8
Write-DeadlineLog "Final log saved to: $script:logFile"

# Exit with appropriate code
exit $scriptExitCode
pause
