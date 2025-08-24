$source = "C:\AI\ComfyUI_windows_portable3"
$destination = "X:\AI\ComfyUI_windows_portable3"
$logDir = "X:\scripts\copy_comfy_to_network_logs"

# Get current date and time in YYYY-MM-DD_HH-MM-SS format
$dateStr = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logFile = "$logDir\copy_log_$dateStr.txt"

# Ensure log directory exists
if (!(Test-Path -Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force
}

# Start transcript to capture all console output
Start-Transcript -Path $logFile -Append

# Create destination if it doesn't exist
if (!(Test-Path -Path $destination)) {
    New-Item -ItemType Directory -Path $destination
}

# --- Extra exclusions for robocopy ---
$extraExcludeDirs = @()
$extraExcludeDirs += 'LTX-Video-Q8-Kernels'
$extraExcludeDirs += 'SageAttention'
$extraExcludeDirs += 'temp'
# Recursively find folders whose name contains 'triton' or 'sageattention' (case-insensitive)
$foldersToExclude = Get-ChildItem -Path $source -Directory -Recurse | Where-Object { $_.Name -match 'triton' -or $_.Name -match 'sageattention' }
foreach ($folder in $foldersToExclude) {
    $extraExcludeDirs += $folder.FullName
}
$allExcludeDirs = @('__pycache__', 'output', 'input') + $extraExcludeDirs

# Main robocopy with all exclusions
robocopy $source $destination /MIR /XD $allExcludeDirs /XF "*.md5" "*.log" "*.tmp" /R:5 /W:5 /NFL /NDL /NP

# Copy the specific example.png file
$exampleFile = "$source\ComfyUI\input\example.png"
$inputDestination = "$destination\ComfyUI\input"
if (Test-Path $exampleFile) {
    # Ensure input directory exists
    if (!(Test-Path $inputDestination)) {
        New-Item -ItemType Directory -Path $inputDestination -Force
    }
    Copy-Item $exampleFile $inputDestination -Force
}

# Copy all subfolders of input (with their contents) but exclude root input files
$inputSource = "$source\ComfyUI\input"
if (Test-Path $inputSource) {
    # Copy only subdirectories and their contents from input folder
    robocopy $inputSource $inputDestination /S /XF * /R:2 /W:2 /NFL /NDL /NP
    # Then copy contents of subdirectories
    Get-ChildItem $inputSource -Directory | ForEach-Object {
        robocopy $_.FullName "$inputDestination\$($_.Name)" /E /R:2 /W:2 /NFL /NDL /NP
    }
}

# Create empty output folder structure
$outputSource = "$source\ComfyUI\output"
$outputDestination = "$destination\ComfyUI\output"
if (Test-Path $outputSource) {
    # Copy directory structure without files
    robocopy $outputSource $outputDestination /E /XF * /R:2 /W:2 /NFL /NDL /NP
}

Write-Host "ComfyUI copied with custom input/output handling:"
Write-Host "- Copied example.png specifically"
Write-Host "- Copied all input subfolders with contents"
Write-Host "- Created empty output folder structure"

# Stop transcript
Stop-Transcript
