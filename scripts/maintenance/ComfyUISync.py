from __future__ import absolute_import, print_function

r"""
ComfyUI portable installation sync maintenance script.

Mirrors a shared ComfyUI portable install to a local Worker path
using Robocopy /MIR, excluding user-specific input/ and output/ folders so
their contents are never erased. `.git` is included so workers can keep
the same Git metadata as the share. Only ComfyUI/input/example.png is copied separately
(as required for ComfyUI to work properly).

Designed to be launched on Deadline Workers via the CommandLine or
DeadlineCommand plugin,
so all stdout/stderr (or Deadline logging if available) ends up in job logs.
"""

import os
import subprocess
import sys
import time
from datetime import datetime

SOURCE_DIR = os.environ.get("COMFY_SYNC_SOURCE", r"\\YOUR-SERVER\share\AI\ComfyUI_windows_portable")
DEST_DIR = os.environ.get("COMFY_SYNC_DEST", r"C:\AI\ComfyUI_windows_portable")
ROBOCOPY_EXECUTABLE = "robocopy"
ROBOCOPY_FLAGS = [
    "/MIR",  # Mirror source to destination (adds + deletes)
    "/FFT",  # Assume FAT file times (two-second granularity) for cross-protocol copies
    "/Z",    # Restartable mode
    "/R:3",  # Retry failed copies 3 times
    "/W:5",  # Wait 5 seconds between retries
    "/NFL",  # No file list (keeps logs smaller)
    "/NDL",  # No directory list
]
EXCLUDED_FILE_PATTERNS = ["*.log", "*.tmp"]
PUBLISH_LOCK_FILE = SOURCE_DIR + ".sync_in_progress"
PUBLISH_LOCK_POLL_SECONDS = 10
PUBLISH_LOCK_TIMEOUT_SECONDS = 60 * 60
PUBLISH_LOCK_STALE_SECONDS = 6 * 60 * 60
EXCLUDED_DIRS = [
    "__pycache__",
    "node_modules",
    "logs",
    os.path.join(SOURCE_DIR, "ComfyUI", "input"),
    os.path.join(SOURCE_DIR, "ComfyUI", "output"),
]

try:
    from Deadline.Scripting import ClientUtils  # type: ignore
except Exception:  # pragma: no cover - Deadline libs unavailable outside Worker
    ClientUtils = None


def log(message):
    """Log to Deadline if available, otherwise stdout."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[ComfyUISync] {0} {1}".format(timestamp, message)
    if ClientUtils:
        ClientUtils.LogText(line)
    else:
        print(line)


def ensure_destination():
    """Ensure destination directory exists."""
    if not os.path.isdir(DEST_DIR):
        log("Destination directory does not exist, creating: {0}".format(DEST_DIR))
        os.makedirs(DEST_DIR)


def wait_for_publish_lock():
    """Avoid copying the share while it is being refreshed from the source machine."""
    start_time = time.time()
    while os.path.exists(PUBLISH_LOCK_FILE):
        try:
            lock_age = time.time() - os.path.getmtime(PUBLISH_LOCK_FILE)
        except OSError:
            continue

        if lock_age >= PUBLISH_LOCK_STALE_SECONDS:
            log(
                "Ignoring stale publish lock older than {0} seconds: {1}".format(
                    PUBLISH_LOCK_STALE_SECONDS,
                    PUBLISH_LOCK_FILE,
                )
            )
            return

        elapsed = time.time() - start_time
        if elapsed >= PUBLISH_LOCK_TIMEOUT_SECONDS:
            raise RuntimeError(
                "Timed out waiting for publish lock to clear: {0}".format(
                    PUBLISH_LOCK_FILE
                )
            )

        log("Publish lock present, waiting before sync: {0}".format(PUBLISH_LOCK_FILE))
        time.sleep(PUBLISH_LOCK_POLL_SECONDS)


def mirror_comfyui():
    """Run robocopy mirror operation."""
    cmd = [ROBOCOPY_EXECUTABLE, SOURCE_DIR, DEST_DIR] + ROBOCOPY_FLAGS
    if EXCLUDED_FILE_PATTERNS:
        cmd.append("/XF")
        cmd.extend(EXCLUDED_FILE_PATTERNS)
    if EXCLUDED_DIRS:
        cmd.append("/XD")
        cmd.extend(EXCLUDED_DIRS)
    log("Running command: {0}".format(" ".join(cmd)))
    process = subprocess.Popen(cmd)
    process.wait()
    exit_code = process.returncode

    # Robocopy exit codes < 8 are success (0=No Change, 1=Copied, etc.)
    if exit_code >= 8:
        msg = "Robocopy failed with exit code {0}".format(exit_code)
        log(msg)
        raise RuntimeError(msg)

    log("Robocopy completed successfully with exit code {0}".format(exit_code))


def ensure_example_png():
    """Copy ComfyUI/input/example.png from source to destination. Required for ComfyUI to work properly."""
    src_file = os.path.join(SOURCE_DIR, "ComfyUI", "input", "example.png")
    dest_dir = os.path.join(DEST_DIR, "ComfyUI", "input")

    if not os.path.isfile(src_file):
        log("Source example.png not found, skipping: {0}".format(src_file))
        return

    if not os.path.isdir(dest_dir):
        log("Creating input directory: {0}".format(dest_dir))
        os.makedirs(dest_dir)

    cmd = [
        ROBOCOPY_EXECUTABLE,
        os.path.join(SOURCE_DIR, "ComfyUI", "input"),
        dest_dir,
        "example.png",
        "/NFL", "/NDL",
    ]
    log("Ensuring input/example.png exists: {0}".format(" ".join(cmd)))
    process = subprocess.Popen(cmd)
    process.wait()
    exit_code = process.returncode
    if exit_code >= 8:
        log("Warning: Failed to copy example.png (exit code {0})".format(exit_code))
    else:
        log("example.png ensured successfully.")


def main():
    log("ComfyUI portable installation sync starting.")
    log("Source: {0}".format(SOURCE_DIR))
    log("Destination: {0}".format(DEST_DIR))

    wait_for_publish_lock()
    ensure_destination()
    mirror_comfyui()
    ensure_example_png()

    log("ComfyUI portable installation sync finished.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - runtime safeguard
        log("ERROR: {0}".format(exc))
        raise


def __main__(*args):  # Deadline's ExecuteScript entry point
    main()
