#!/usr/bin/env python
from __future__ import absolute_import, print_function

r"""
Utility script that submits ComfyUI sync jobs as Deadline maintenance jobs.

Supports two types of sync:
1. ComfyUI portable installation sync (mirrors a shared ComfyUI portable install to local Workers)
2. ComfyUI models sync (syncs AI models from shared storage to local Worker drives, using modellist.txt)

Defaults are publication-safe examples:
- Pool none / group none / no region label
- Maintenance flag enabled
- Empty machine allow list by default
- DeadlineCommand plugin executes sync scripts via deadlinecommand -ExecuteScript

Usage examples:
    # Submit both ComfyUI installation and models sync
    python maintenance/submit_comfy_sync.py

    # Submit only ComfyUI installation sync
    python maintenance/submit_comfy_sync.py --type installation

    # Submit only models sync
    python maintenance/submit_comfy_sync.py --type models

    # Custom settings
    python maintenance/submit_comfy_sync.py --pool mypool --allowlist "GPU01,GPU02"
"""

import argparse
import io
import locale
import os
import subprocess
import sys
import tempfile

# Default configuration
DEFAULT_POOL = "none"
DEFAULT_GROUP = "none"
DEFAULT_REGION = ""
DEFAULT_PRIORITY = 100
DEFAULT_WORKER_DEADLINE_COMMAND = "deadlinecommand"
DEFAULT_ALLOWLIST = ()
DEFAULT_ALLOWLIST_STRING = ",".join(DEFAULT_ALLOWLIST)

# Script paths
COMFYUI_SYNC_SCRIPT = r"\\YOUR-SERVER\share\scripts\maintenance\ComfyUISync.py"
COMFY_MODELS_SYNC_SCRIPT = r"\\YOUR-SERVER\share\scripts\maintenance\ComfyModelsSync.py"


def normalize_allowlist(raw_value):
    """Return a Deadline-ready comma-separated allowlist string and token list."""
    if not raw_value:
        return "", []
    tokens = []
    for chunk in raw_value.replace("\n", ",").split(","):
        candidate = chunk.strip()
        if candidate:
            tokens.append(candidate)
    return ",".join(tokens), tokens


def parse_args():
    parser = argparse.ArgumentParser(
        description="Submit ComfyUI sync maintenance jobs to Deadline."
    )
    parser.add_argument(
        "--type",
        choices=["both", "installation", "models"],
        default="both",
        help="Type of sync job to submit (default: both)"
    )
    parser.add_argument("--job-name", help="Base job name (will be suffixed with sync type)")
    parser.add_argument(
        "--comment",
        help="Base job comment/description (will be suffixed with sync type)",
    )
    parser.add_argument("--pool", default=DEFAULT_POOL, help="Deadline pool.")
    parser.add_argument("--group", default=DEFAULT_GROUP, help="Deadline group.")
    parser.add_argument("--region", default=DEFAULT_REGION, help="Deadline region label.")
    parser.add_argument("--priority", type=int, default=DEFAULT_PRIORITY, help="Job priority (0-100).")
    parser.add_argument(
        "--machine-limit",
        type=int,
        help="Limit each job to N Workers (default: auto = size of allow list).",
    )
    parser.add_argument(
        "--allowlist",
        default=DEFAULT_ALLOWLIST_STRING,
        help="Comma or newline separated Worker names allowed to run the jobs.",
    )
    parser.add_argument(
        "--comfyui-script",
        default=COMFYUI_SYNC_SCRIPT,
        help="UNC path to ComfyUISync.py accessible by Workers.",
    )
    parser.add_argument(
        "--models-script",
        default=COMFY_MODELS_SYNC_SCRIPT,
        help="UNC path to ComfyModelsSync.py accessible by Workers.",
    )
    parser.add_argument(
        "--additional-arguments",
        default="",
        help="Extra arguments passed to sync scripts (optional).",
    )
    parser.add_argument(
        "--deadline-command",
        default=None,
        help="Path to deadlinecommand executable for submitting jobs. "
             "Defaults to DEADLINE_PATH or PATH lookup.",
    )
    parser.add_argument(
        "--worker-deadline-command",
        default=DEFAULT_WORKER_DEADLINE_COMMAND,
        help="Executable that Workers should run inside the DeadlineCommand plugin.",
    )
    parser.add_argument(
        "--suspended",
        action="store_true",
        help="Submit jobs in suspended state (resume manually).",
    )
    parser.add_argument(
        "--user-name",
        default="",
        help="Override the Deadline job owner (leave blank to use current user).",
    )
    parser.add_argument(
        "--maintenance",
        dest="maintenance",
        action="store_true",
        help="Mark as maintenance jobs (default).",
    )
    parser.add_argument(
        "--no-maintenance",
        dest="maintenance",
        action="store_false",
        help="Submit without the MaintenanceJob flag.",
    )
    parser.set_defaults(maintenance=True)
    return parser.parse_args()


def find_deadline_command(explicit_path):
    if explicit_path:
        return explicit_path

    env_path = os.environ.get("DEADLINE_PATH")
    candidates = []
    if env_path:
        candidates.append(os.path.join(env_path, "deadlinecommand.exe"))
        candidates.append(os.path.join(env_path, "deadlinecommand"))

    candidates.append("deadlinecommand")

    for candidate in candidates:
        if candidate == "deadlinecommand":
            return candidate
        if os.path.isfile(candidate):
            return candidate

    return "deadlinecommand"


def create_temp_file(lines, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with io.open(fd, "w", encoding="utf-8") as handle:
            handle.write(u"\n".join(lines))
            handle.write(u"\n")
    except Exception:
        os.close(fd)
        os.unlink(path)
        raise
    return path


def build_job_info(args, job_name, comment):
    lines = [
        "Plugin=DeadlineCommand",
        "Name={0}".format(job_name),
        "Comment={0}".format(comment),
        "Pool={0}".format(args.pool),
        "Group={0}".format(args.group),
        "Region={0}".format(args.region),
        "Priority={0}".format(args.priority),
        "MachineLimit={0}".format(args.machine_limit),
        "MaintenanceJob={0}".format("true" if args.maintenance else "false"),
        "OnJobComplete=Nothing",
    ]
    if args.allowlist:
        lines.append("Whitelist={0}".format(args.allowlist))
    if args.user_name:
        lines.append("UserName={0}".format(args.user_name))
    if args.suspended:
        lines.append("SubmitSuspended=true")
    return lines


def build_plugin_info(args, script_path):
    argument_parts = ["-ExecuteScript", '"{0}"'.format(script_path)]
    if args.additional_arguments:
        argument_parts.append(args.additional_arguments)

    arguments = " ".join(argument_parts)
    lines = [
        "Executable={0}".format(args.worker_deadline_command),
        "Arguments={0}".format(arguments),
        "ShellExecute=False",
        "StartupDirectory=",
        "SingleFramesOnly=False",
        "HideWindow=False",
        "IgnoreExitCode=False",
    ]
    return lines


def submit_job(deadline_command, job_info_path, plugin_info_path, job_name):
    cmd = [deadline_command, "SubmitJob", job_info_path, plugin_info_path]
    print("[submit_comfy_sync] Submitting job '{0}'...".format(job_name))
    print("[submit_comfy_sync] Running:", " ".join(cmd))

    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout_bytes, stderr_bytes = process.communicate()
    encoding = locale.getpreferredencoding(False) or "utf-8"

    stdout_text = stdout_bytes.decode(encoding, "ignore") if isinstance(stdout_bytes, bytes) else stdout_bytes
    stderr_text = stderr_bytes.decode(encoding, "ignore") if isinstance(stderr_bytes, bytes) else stderr_bytes

    if process.returncode != 0:
        raise RuntimeError(
            "deadlinecommand failed (code {0}):\nSTDOUT:\n{1}\nSTDERR:\n{2}".format(
                process.returncode, stdout_text, stderr_text
            )
        )

    print(stdout_text.strip())
    return stdout_text


def submit_comfyui_sync(args, deadline_command):
    """Submit ComfyUI installation sync job."""
    job_name = args.job_name or "ComfyUI Installation Sync"
    comment = args.comment or "Mirrors ComfyUI portable installation from shared storage to local Workers"

    print("\n[submit_comfy_sync] Preparing ComfyUI installation sync job...")

    if not os.path.exists(args.comfyui_script):
        print(
            "[submit_comfy_sync] Warning: ComfyUI sync script not found right now ({0}). "
            "Ensure Workers can reach this path.".format(args.comfyui_script)
        )

    job_info_lines = build_job_info(args, job_name, comment)
    plugin_info_lines = build_plugin_info(args, args.comfyui_script)

    job_info_path = create_temp_file(job_info_lines, ".job")
    plugin_info_path = create_temp_file(plugin_info_lines, ".plugin")

    try:
        result = submit_job(deadline_command, job_info_path, plugin_info_path, job_name)
        return result
    finally:
        for temp_path in (job_info_path, plugin_info_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def submit_models_sync(args, deadline_command):
    """Submit ComfyUI models sync job."""
    job_name = (args.job_name + " - Models" if args.job_name else "ComfyUI Models Sync")
    comment = (args.comment + " - Models" if args.comment else "Syncs ComfyUI AI models to local Worker storage based on modellist.txt")

    print("\n[submit_comfy_sync] Preparing ComfyUI models sync job...")

    if not os.path.exists(args.models_script):
        print(
            "[submit_comfy_sync] Warning: Models sync script not found right now ({0}). "
            "Ensure Workers can reach this path.".format(args.models_script)
        )

    job_info_lines = build_job_info(args, job_name, comment)
    plugin_info_lines = build_plugin_info(args, args.models_script)

    job_info_path = create_temp_file(job_info_lines, ".job")
    plugin_info_path = create_temp_file(plugin_info_lines, ".plugin")

    try:
        result = submit_job(deadline_command, job_info_path, plugin_info_path, job_name)
        return result
    finally:
        for temp_path in (job_info_path, plugin_info_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def main():
    args = parse_args()
    allowlist_str, allowlist_tokens = normalize_allowlist(args.allowlist)
    args.allowlist = allowlist_str
    allowlist_count = len(allowlist_tokens)
    if args.machine_limit is None:
        args.machine_limit = allowlist_count if allowlist_count else 0

    print("[submit_comfy_sync] ComfyUI sync job submission starting...")
    print("[submit_comfy_sync] Pool: {0}".format(args.pool))
    print("[submit_comfy_sync] Group: {0}".format(args.group))
    print("[submit_comfy_sync] Region: {0}".format(args.region))
    print("[submit_comfy_sync] Allowlist: {0} workers".format(allowlist_count))
    print("[submit_comfy_sync] Sync type: {0}".format(args.type))

    deadline_command = find_deadline_command(args.deadline_command)

    jobs_submitted = []

    try:
        if args.type in ["both", "installation"]:
            result = submit_comfyui_sync(args, deadline_command)
            jobs_submitted.append(("ComfyUI Installation", result))

        if args.type in ["both", "models"]:
            result = submit_models_sync(args, deadline_command)
            jobs_submitted.append(("ComfyUI Models", result))

        print("\n[submit_comfy_sync] Job submission completed successfully!")
        print("[submit_comfy_sync] Jobs submitted: {0}".format(len(jobs_submitted)))
        for job_type, result in jobs_submitted:
            print("[submit_comfy_sync] - {0}: {1}".format(job_type, result.strip() if result else "Unknown"))

    except Exception as e:
        print("[submit_comfy_sync] ERROR: {0}".format(e))
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print("[submit_comfy_sync] ERROR:", exc)
        sys.exit(1)
