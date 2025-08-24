# Copy ComfyUI Script - No Confirmation Version
# Designed for Deadline Execute Command

$sourcePath = "X:\AI\ComfyUI_windows_portable3"
$destPath = "C:\AI\ComfyUI_windows_portable3"
$logDir = "X:\scripts\copy_comfy_to_local_logs"
$computerName = $env:COMPUTERNAME

# Get current date and time for log file naming
$currentDate = Get-Date -Format "yyyyMMdd-HHmmss"
$script:logFile = $null  # initialize

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

Write-DeadlineLog "=== COPY COMFYUI SCRIPT STARTED ==="
Write-DeadlineLog "Script running on: $env:COMPUTERNAME"

# Create log directory if it doesn't exist
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    Write-DeadlineLog "Created log directory: $logDir"
}

Write-DeadlineLog "Copying ComfyUI from X: to C: drive..."
Write-DeadlineLog "Source: $sourcePath"
Write-DeadlineLog "Destination: $destPath"

# Check if source exists
if (-not (Test-Path $sourcePath)) {
    # Set FAIL log file
    $script:logFile = "$logDir\$computerName-$currentDate-FAIL.txt"
    "" | Out-File -FilePath $script:logFile -Encoding UTF8  # Clear/create file
    Write-DeadlineLog "ERROR: Source path $sourcePath not found!" "Error"
    Write-DeadlineLog "Log saved to: $script:logFile"
    exit 1
}

Write-DeadlineLog "Source found - starting copy operation..."

try {
    # Create the destination directory if it doesn't exist (equivalent to mkdir C:\AI)
    $destParent = Split-Path $destPath -Parent
    if (-not (Test-Path $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
        Write-DeadlineLog "Created directory: $destParent"
    }
    
    # Use robocopy for mirroring (mirror = /MIR)
    Write-DeadlineLog "Starting robocopy operation..."
    $robocopyArgs = @($sourcePath, $destPath, "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    $output = robocopy @robocopyArgs

    $exitCode = $LASTEXITCODE

    # Robocopy exit codes: 0-3 are success, 4+ are errors
    if ($exitCode -le 3) {
        $script:logFile = "$logDir\$computerName-$currentDate-SUCCESS.txt"
        Write-DeadlineLog "Copy completed successfully! (exit code: $exitCode)"
        $finalStatus = "SUCCESS"
        $scriptExitCode = 0
        
        # Log robocopy output if available
        if ($output) {
            $outputString = $output -join " "
            if ($outputString.Trim() -ne "") {
                Write-DeadlineLog "Robocopy details: $outputString"
            }
        }
    } else {
        $script:logFile = "$logDir\$computerName-$currentDate-FAIL.txt"
        Write-DeadlineLog "Copy failed with error code: $exitCode" "Error"
        $finalStatus = "FAIL"
        $scriptExitCode = 1
        
        if ($output) {
            Write-DeadlineLog "Robocopy error output: $($output -join ' ')" "Error"
        }
    }
    
} catch {
    $script:logFile = "$logDir\$computerName-$currentDate-FAIL.txt"
    Write-DeadlineLog "ERROR: Exception during copy operation - $($_.Exception.Message)" "Error"
    $finalStatus = "FAIL"
    $scriptExitCode = 1
}

Write-DeadlineLog "=== COPY OPERATION COMPLETED ==="

# Create final summary log
$summaryLog = @"
=== COPY COMFYUI SCRIPT COMPLETED ===
Computer: $computerName
Date: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
Status: $finalStatus

SOURCE: $sourcePath
DESTINATION: $destPath

Script completed with status: $finalStatus
"@

$summaryLog | Out-File -FilePath $script:logFile -Encoding UTF8
Write-DeadlineLog "Log saved to: $script:logFile"

exit $scriptExitCode
