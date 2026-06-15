from __future__ import absolute_import, print_function

r"""
ComfyUI models sync maintenance script.

Syncs AI models from shared storage to a local drive based on computer name.
- Computers in E_DRIVE_COMPUTERS copy to E:\AI\models
- Computers in D_DRIVE_COMPUTERS copy to D:\AI\models
- All other computers copy to C:\AI\models
Uses the same model list (modellist.txt) for all computers.

Features:
- Computer-specific drive selection
- Delta-aware space checking with 15% safety buffer
- Delta-aware copying (only copies changed/missing files)
- Cleans up files not in the model list
- Uses Robocopy for reliable file operations
- Comprehensive logging for Deadline integration

Designed to be launched on Deadline Workers via the CommandLine or
DeadlineCommand plugin,
so all stdout/stderr (or Deadline logging if available) ends up in job logs.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime

# Configuration
SOURCE_DIR = os.environ.get("COMFY_MODELS_SOURCE", r"\\YOUR-SERVER\share\AI\models")
MODEL_LIST_PATH = os.environ.get("COMFY_MODEL_LIST", r"\\YOUR-SERVER\share\scripts\modellist.txt")

# Drive configuration by computer
# Add Worker hostnames here for site-specific drive placement.
D_DRIVE_COMPUTERS = set()
E_DRIVE_COMPUTERS = set()
ROBOCOPY_EXECUTABLE = "robocopy"
ROBOCOPY_FLAGS = [
    "/R:10",  # Retry 10 times
    "/W:30",  # Wait 30 seconds between retries
    "/V",     # Verbose output
    "/TS",    # Include source time stamps
    "/FP",    # Include full path names
    "/NP",    # No progress indicator
    "/MT:1"   # Single-threaded for reliability
]

try:
    from Deadline.Scripting import ClientUtils  # type: ignore
except Exception:  # pragma: no cover - Deadline libs unavailable outside Worker
    ClientUtils = None


def log(message):
    """Log to Deadline if available, otherwise stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[ComfyModelsSync] {0} {1}".format(timestamp, message)
    if ClientUtils:
        ClientUtils.LogText(line)
    else:
        print(line)


def get_drive_space_info(dest_dir):
    """Get available space information for destination drive."""
    import shutil

    try:
        # Get drive letter and root path
        drive_letter = dest_dir.split(':')[0].upper()
        drive_root = "{0}:\\".format(drive_letter)

        # Get disk usage stats using cross-platform method
        total_bytes, used_bytes, free_bytes = shutil.disk_usage(drive_root)

        free_gb = free_bytes / (1024**3)
        total_gb = total_bytes / (1024**3)

        return {
            'free_bytes': free_bytes,
            'free_gb': round(free_gb, 2),
            'total_gb': round(total_gb, 2)
        }
    except Exception as e:
        log("WARNING: Could not get drive space info: {0}".format(e))
        return None


def calculate_required_space(valid_model_paths, dest_dir):
    """Calculate space required for files that need copying (delta-aware)."""
    total_delta_bytes = 0
    files_to_copy = []

    log("Analyzing space requirements (delta-aware)...")

    for source_path in valid_model_paths:
        # Calculate destination path
        if source_path.startswith(SOURCE_DIR):
            relative_path = source_path[len(SOURCE_DIR):].lstrip(os.sep)
        else:
            relative_path = os.path.basename(source_path)

        dest_path = os.path.join(dest_dir, relative_path)

        # Check if file needs copying
        needs_copy_flag, _ = needs_copy(source_path, dest_path)

        if needs_copy_flag:
            try:
                file_size = os.path.getsize(source_path)
                total_delta_bytes += file_size
                files_to_copy.append(source_path)
            except OSError:
                log("WARNING: Could not get size for {0}".format(source_path))

    total_delta_gb = round(total_delta_bytes / (1024**3), 2)

    log("Space analysis complete:")
    log("  Files requiring copy: {0}".format(len(files_to_copy)))
    log("  Total space required (delta): {0} GB".format(total_delta_gb))

    return {
        'total_delta_bytes': total_delta_bytes,
        'total_delta_gb': total_delta_gb,
        'files_to_copy': files_to_copy
    }


def get_computer_config():
    """Determine destination drive based on computer name."""
    import socket

    # Get computer name
    computer_name = socket.gethostname().upper()
    log("Running on computer: {0}".format(computer_name))

    # Determine drive
    if computer_name in E_DRIVE_COMPUTERS:
        dest_drive = "E"
    elif computer_name in D_DRIVE_COMPUTERS:
        dest_drive = "D"
    else:
        dest_drive = "C"

    dest_dir = "{0}:\\AI\\models".format(dest_drive)

    log("Using destination drive: {0} (directory: {1})".format(dest_drive, dest_dir))

    return dest_dir


def read_model_list():
    """Read and parse the model list file."""
    if not os.path.exists(MODEL_LIST_PATH):
        raise RuntimeError("Model list file not found: {0}".format(MODEL_LIST_PATH))

    with open(MODEL_LIST_PATH, 'r') as f:
        lines = f.readlines()

    # Parse paths, skip empty lines
    model_paths = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#'):
            model_paths.append(line)

    log("Loaded {0} model paths from list".format(len(model_paths)))
    return model_paths


def ensure_destination():
    """Ensure destination directory exists."""
    if not os.path.isdir(DEST_DIR):
        log("Destination directory does not exist, creating: {0}".format(DEST_DIR))
        os.makedirs(DEST_DIR)


def get_file_size_mb(file_path):
    """Get file size in MB."""
    try:
        size_bytes = os.path.getsize(file_path)
        return size_bytes / (1024 * 1024)
    except OSError:
        return 0


def analyze_models(model_paths):
    """Analyze model files and return valid/missing lists."""
    valid_files = []
    missing_files = []

    for model_path in model_paths:
        if os.path.exists(model_path):
            valid_files.append(model_path)
        else:
            missing_files.append(model_path)
            log("WARNING: Model file not found: {0}".format(model_path))

    total_size_mb = sum(get_file_size_mb(path) for path in valid_files)

    log("Analysis complete:")
    log("  Total models in list: {0}".format(len(model_paths)))
    log("  Valid source files: {0}".format(len(valid_files)))
    log("  Missing source files: {0}".format(len(missing_files)))
    log("  Total size of valid files: {0:.2f} MB".format(total_size_mb))

    return valid_files, missing_files


def cleanup_extra_files(valid_model_paths):
    """Remove files from destination that are not in the model list."""
    if not os.path.exists(DEST_DIR):
        log("Destination directory does not exist yet - no cleanup needed")
        return 0, 0

    # Build set of expected relative paths
    expected_relative_paths = set()
    for model_path in valid_model_paths:
        if model_path.startswith(SOURCE_DIR):
            relative_path = model_path[len(SOURCE_DIR):].lstrip(os.sep)
            expected_relative_paths.add(relative_path)

    # Find all files in destination
    removed_count = 0
    removed_size_mb = 0

    for root, dirs, files in os.walk(DEST_DIR):
        for file in files:
            full_path = os.path.join(root, file)
            relative_path = os.path.relpath(full_path, DEST_DIR)

            if relative_path not in expected_relative_paths:
                try:
                    size_mb = get_file_size_mb(full_path)
                    os.remove(full_path)
                    log("Removed extra file: {0} ({1:.2f} MB)".format(relative_path, size_mb))
                    removed_count += 1
                    removed_size_mb += size_mb
                except OSError as e:
                    log("WARNING: Failed to remove {0}: {1}".format(relative_path, e))

    # Clean up empty directories
    for root, dirs, files in os.walk(DEST_DIR, topdown=False):
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    log("Removed empty directory: {0}".format(os.path.relpath(dir_path, DEST_DIR)))
            except OSError:
                pass  # Directory not empty or other error

    if removed_count > 0:
        log("Cleanup completed: {0} files removed ({1:.2f} MB freed)".format(removed_count, removed_size_mb))
    else:
        log("Cleanup completed: No extra files found")

    return removed_count, removed_size_mb


def needs_copy(source_path, dest_path):
    """Check if file needs to be copied."""
    if not os.path.exists(dest_path):
        return True, "missing"

    try:
        source_stat = os.stat(source_path)
        dest_stat = os.stat(dest_path)

        if source_stat.st_mtime > dest_stat.st_mtime:
            return True, "newer"
        elif source_stat.st_size != dest_stat.st_size:
            return True, "different size"
        else:
            return False, "up to date"
    except OSError:
        return True, "error checking"


def copy_model_file(source_path, dest_path):
    """Copy a model file using robocopy."""
    source_dir = os.path.dirname(source_path)
    dest_dir = os.path.dirname(dest_path)
    file_name = os.path.basename(source_path)

    # Ensure destination directory exists (including subdirectories)
    os.makedirs(dest_dir, exist_ok=True)

    cmd = [ROBOCOPY_EXECUTABLE, source_dir, dest_dir, file_name] + ROBOCOPY_FLAGS

    log("Running: {0}".format(" ".join(cmd)))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = process.communicate()

    if process.returncode >= 8:
        error_msg = "Robocopy failed with exit code {0}".format(process.returncode)
        if stdout:
            error_msg += "\nSTDOUT: {0}".format(stdout.strip())
        if stderr:
            error_msg += "\nSTDERR: {0}".format(stderr.strip())
        raise RuntimeError(error_msg)

    return process.returncode


def sync_models(valid_model_paths):
    """Sync all valid model files."""
    copied_count = 0
    skipped_count = 0
    error_count = 0
    total_processed = 0

    for source_path in valid_model_paths:
        total_processed += 1

        # Calculate destination path
        if source_path.startswith(SOURCE_DIR):
            relative_path = source_path[len(SOURCE_DIR):].lstrip(os.sep)
        else:
            relative_path = os.path.basename(source_path)

        dest_path = os.path.join(DEST_DIR, relative_path)

        # Check if copy is needed
        needs_copy_flag, reason = needs_copy(source_path, dest_path)

        if needs_copy_flag:
            try:
                size_mb = get_file_size_mb(source_path)
                log("Copying ({0}): {1} ({2:.2f} MB)".format(reason, relative_path, size_mb))

                exit_code = copy_model_file(source_path, dest_path)

                if exit_code <= 7:  # Success
                    log("SUCCESS: {0} (exit code: {1})".format(relative_path, exit_code))
                    copied_count += 1
                else:
                    log("WARNING: {0} completed with exit code {1}".format(relative_path, exit_code))
                    copied_count += 1

            except Exception as e:
                log("ERROR: Failed to copy {0}: {1}".format(relative_path, e))
                error_count += 1
        else:
            log("Skipped (up to date): {0}".format(relative_path))
            skipped_count += 1

        # Progress update
        if total_processed % 10 == 0 or total_processed == len(valid_model_paths):
            percent = (total_processed * 100) / len(valid_model_paths)
            log("Progress: {0:.1f}% ({1}/{2}) | Copied: {3} | Skipped: {4} | Errors: {5}".format(
                percent, total_processed, len(valid_model_paths), copied_count, skipped_count, error_count))

    return copied_count, skipped_count, error_count


def main():
    log("ComfyUI models sync starting.")
    log("Source directory: {0}".format(SOURCE_DIR))
    log("Model list: {0}".format(MODEL_LIST_PATH))

    try:
        # Get computer-specific configuration
        dest_dir = get_computer_config()

        # Make dest_dir available globally for other functions
        global DEST_DIR
        DEST_DIR = dest_dir

        # Read model list
        model_paths = read_model_list()

        # Analyze models
        valid_model_paths, missing_files = analyze_models(model_paths)

        # Perform space checking (delta-aware)
        space_info = calculate_required_space(valid_model_paths, dest_dir)
        drive_info = get_drive_space_info(dest_dir)

        if drive_info and space_info:
            # Apply 15% safety buffer like PowerShell script
            buffer_multiplier = 1.15
            required_with_buffer = int(space_info['total_delta_bytes'] * buffer_multiplier)
            required_with_buffer_gb = round(required_with_buffer / (1024**3), 2)

            log("Space check:")
            log("  Required (with 15% buffer): {0} GB".format(required_with_buffer_gb))
            log("  Available: {0} GB".format(drive_info['free_gb']))

            if drive_info['free_bytes'] < required_with_buffer:
                shortfall_gb = round((required_with_buffer - drive_info['free_bytes']) / (1024**3), 2)
                log("ERROR: Insufficient disk space!")
                log("  Shortfall: {0} GB".format(shortfall_gb))
                raise RuntimeError("Insufficient disk space for model sync")
            else:
                log("Space check PASSED")
        else:
            log("WARNING: Could not perform space check - proceeding anyway")

        # Ensure destination exists
        ensure_destination()

        # Cleanup extra files
        cleanup_extra_files(valid_model_paths)

        # Sync models
        copied_count, skipped_count, error_count = sync_models(valid_model_paths)

        # Final summary
        log("Sync completed:")
        log("  Models in list: {0}".format(len(model_paths)))
        log("  Valid source files: {0}".format(len(valid_model_paths)))
        log("  Missing source files: {0}".format(len(missing_files)))
        log("  Files copied: {0}".format(copied_count))
        log("  Files skipped: {0}".format(skipped_count))
        log("  Files with errors: {0}".format(error_count))

        if error_count > 0:
            log("WARNING: Some files failed to copy")
            sys.exit(1)

    except Exception as e:
        log("ERROR: {0}".format(e))
        sys.exit(1)

    log("ComfyUI models sync finished successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - runtime safeguard
        log("ERROR: {0}".format(exc))
        raise


def __main__(*args):  # Deadline's ExecuteScript entry point
    main()
