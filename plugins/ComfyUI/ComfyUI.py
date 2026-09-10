from __future__ import absolute_import
from Deadline.Plugins import DeadlinePlugin, PluginType
from System.Diagnostics import ProcessPriorityClass
from Deadline.Scripting import RepositoryUtils, SystemUtils, FileUtils
import os
import re 
import sys
import json
import time
import socket
import threading
import urllib.request
import urllib.error
import urllib.parse
import traceback
import random
import platform
import subprocess
import base64
import shutil
import copy
import uuid
from typing import Tuple

"""
ComfyUI Deadline Plugin
by Dominik Bargiel dominikbargiel97@gmail.com

A Deadline plugin for rendering ComfyUI workflows. Handles workflow submission, 
progress monitoring, seed manipulation, batch processing, and multi-GPU support.
Supports both existing ComfyUI instances and launching new ones.
"""

# Constants
DEFAULT_PORT = 8188
PORT_OFFSET_PER_GPU = 100
MAX_PORT_SEARCH_RANGE = 100
DEFAULT_POLLING_INTERVAL = 10  # seconds
MAX_SEED_VALUE = 2147483647
PROGRESS_LOG_INTERVAL = 10  # Log every 10 polls
FILE_WRITE_DELAY = 2  # seconds to wait for files to be written

# Seed parameter names to search for in workflows
SEED_PARAMETER_NAMES = ["seed", "noise_seed", "value"]
DEADLINE_SEED_NODE_TYPES = {"DeadlineSeed", "DeadlineDistributedSeed", "DistributedSeed"}

# Output node types that indicate the workflow will produce output
OUTPUT_NODE_TYPES = ["SaveImage", "PreviewImage", "SaveVideo"]

# Nodes in this set wait for a browser/client decision or consume state that is
# only created by a frontend extension.  Metadata-aware save/display nodes are
# deliberately not included: PROMPT, DYNPROMPT, UNIQUE_ID and EXTRA_PNGINFO are
# normal ComfyUI hidden inputs and are safe when their required metadata exists.
KNOWN_UI_DEPENDENT_NODE_TYPES = {
    "FL_ImagePicker",
    "easy imageChooser",
    "ImageChooser",
    "PreviewChooser",
    "PreviewBridge",
    "ImpactPreviewBridge",
}

WORKFLOW_METADATA_REQUIRED_NODE_TYPES = {
    "WidgetToString",
    "ImpactControlBridge",
}

# Fixed pass-through switches can be removed from the API graph.  The selected
# upstream connection is wired directly into every consumer.  This is a graph
# transform, not a runtime workaround for any individual custom node.
FIXED_SWITCH_SPECS = {
    "ImpactSwitch": {"select": "select", "input": "input{index}", "constants": {1: "input{index}", 2: "index"}},
    "LatentSwitch": {"select": "select", "input": "input{index}"},
    "SEGSSwitch": {"select": "select", "input": "input{index}"},
}

FILE_OUTPUT_GROUPS = ("images", "gifs", "videos", "audio")

def get_distributed_config_for_plugin(plugin) -> Tuple[bool, bool, bool]:
    """Get distributed configuration with plugin info priority, fallback to environment"""
    # Priority 1: Plugin info entries (preferred)
    worker_mode = plugin.GetBooleanPluginInfoEntryWithDefault("WorkerMode", False)
    distributed_mode = plugin.GetBooleanPluginInfoEntryWithDefault("DistributedMode", False) 
    force_new_instance = plugin.GetBooleanPluginInfoEntryWithDefault("ForceNewInstance", False)
    
    # Priority 2: Environment variables (fallback for backwards compatibility)
    if not worker_mode and not distributed_mode and not force_new_instance:
        worker_mode = os.environ.get('COMFY_WORKER_MODE', '0').lower() in ('1', 'true', 'yes')
        distributed_mode = os.environ.get('DEADLINE_DIST_MODE', '0').lower() in ('1', 'true', 'yes')
        force_new_instance = os.environ.get('COMFY_FORCE_NEW_INSTANCE', '0').lower() in ('1', 'true', 'yes')
        
        if worker_mode or distributed_mode or force_new_instance:
            plugin.LogWarning("Using environment variables for distributed config. Consider updating to plugin info entries.")
    
    # Log the configuration
    plugin.LogInfo(f"Distributed config - WorkerMode: {worker_mode}, DistributedMode: {distributed_mode}, ForceNewInstance: {force_new_instance}")
    
    return worker_mode, distributed_mode, force_new_instance

def GetDeadlinePlugin():
    return ComfyUI()

def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()

class ComfyUIError(Exception):
    """Custom exception for ComfyUI plugin errors"""
    pass

class ComfyUI(DeadlinePlugin):
    def __init__(self):
        if sys.version_info.major == 3:
            super().__init__()
            
        self._setup_callbacks()
        self._setup_stdout_handlers()
        self._initialize_member_variables()

    def _setup_callbacks(self):
        """Setup all plugin callbacks"""
        self.InitializeProcessCallback += self.InitializeProcess
        self.RenderExecutableCallback += self.RenderExecutable
        self.RenderArgumentCallback += self.RenderArgument
        self.PreRenderTasksCallback += self.PreRenderTasks
        self.PostRenderTasksCallback += self.PostRenderTasks

    def _setup_stdout_handlers(self):
        """Setup stdout handlers for ComfyUI output parsing"""
        # Server startup handlers
        self.AddStdoutHandlerCallback(".*Starting server.*").HandleCallback += self.HandleServerStarted
        # Also catch the GUI message as backup
        self.AddStdoutHandlerCallback(".*To see the GUI go to.*").HandleCallback += self.HandleServerStarted
        self.AddStdoutHandlerCallback(".*Error:.*").HandleCallback += self.HandleStdoutError
        self.AddStdoutHandlerCallback(".*Exception:.*").HandleCallback += self.HandleStdoutError

        # Progress handlers
        self.AddStdoutHandlerCallback(r"\s*([0-9]+)%\|.*\|\s*([0-9]+)/([0-9]+).*").HandleCallback += self.HandleStdoutProgressBar
        self.AddStdoutHandlerCallback(r"Progress: ([0-9.]+)%.*").HandleCallback += self.HandleStdoutProgressPercent
        # Completion is tracked through /history so Deadline's own timeout policy remains authoritative.

    def _initialize_member_variables(self):
        """Initialize all member variables"""
        # Core state variables
        self.comfyui_output_dir = ""
        self.server_started = False
        self.task_completed = False
        self.comfyui_process = None
        self.comfyui_api_url = None
        self.temp_dir = None
        self.workflow_submitted = False
        self.client_id = None
        self.prompt_id = None
        self.progress_value = 0
        self.thread_running = True
        self.custom_output_dir_specified = False
        self.comfyui_input_dir = ""
        self.input_manifest_file = ""
        self.submission_id = ""
        self.standard_workflow_file = ""
        self.standard_workflow = None
        self.comfyui_install_path = None
        self.comfyui_path_candidates = []
        self.use_existing_comfyui = False
        self.endpoint_policy_active = False
        self.configured_comfyui_api_url = ""
        self.configured_launch_gpu_uuid = ""
        self.reuse_completion_marker = ""
        self.reuse_gui_output_root = ""
        self.reuse_gui_input_root = ""
        self.endpoint_session_id = ""
        self.object_info = None
        self.expected_outputs_by_prompt = {}
        self.submission_error = ""
        
        # Batch processing variables
        self.chunk_size = 1
        self.batch_count = 1
        self.assigned_variation_indices = [0]
        self.prompts_executed = 0
        self.batch_mode = False
        
        # Prompt tracking variables
        self.prompt_ids = []
        self.completed_prompts = set()
        self.current_tracking_index = 0

    def _resolve_comfyui_install_path(self) -> str:
        """Resolve the ComfyUI installation path from multi-entry configuration."""
        if self.comfyui_install_path:
            return self.comfyui_install_path

        config_entry = self.GetConfigEntryWithDefault("ComfyUIPath", "").strip()
        if not config_entry:
            error_msg = "ComfyUIPath configuration is empty. Please specify at least one installation path."
            self.LogWarning(error_msg)
            self.FailRender(error_msg)
            return ""

        raw_candidates = []
        for line in config_entry.splitlines():
            stripped_line = line.strip()
            if not stripped_line:
                continue
            parts = [segment.strip().strip('\"').strip("'") for segment in stripped_line.split(";")]
            raw_candidates.extend([part for part in parts if part])

        if not raw_candidates:
            error_msg = "ComfyUIPath configuration did not yield any usable paths."
            self.LogWarning(error_msg)
            self.FailRender(error_msg)
            return ""

        resolved_candidates = []
        for candidate in raw_candidates:
            try:
                mapped = RepositoryUtils.CheckPathMapping(candidate)
            except Exception as e:
                self.LogWarning(f"Path mapping failed for '{candidate}': {e}")
                mapped = candidate
            expanded = os.path.expandvars(os.path.expanduser(mapped))
            normalized = os.path.normpath(expanded)
            resolved_candidates.append(normalized)

        self.comfyui_path_candidates = resolved_candidates
        self.LogInfo(f"Resolving ComfyUI installation path from {len(resolved_candidates)} candidate(s).")

        for candidate in resolved_candidates:
            if not candidate:
                continue

            python_exe = os.path.join(candidate, "python_embeded", "python.exe")
            comfy_main = os.path.join(candidate, "ComfyUI", "main.py")
            path_exists = os.path.exists(python_exe) and os.path.exists(comfy_main)

            if path_exists:
                self.comfyui_install_path = candidate
                self.LogInfo(f"Using ComfyUI installation at: {candidate}")
                return self.comfyui_install_path

            missing_items = []
            if not os.path.exists(python_exe):
                missing_items.append("python_embeded/python.exe")
            if not os.path.exists(comfy_main):
                missing_items.append("ComfyUI/main.py")
            missing_desc = ", ".join(missing_items) if missing_items else "required files"
            self.LogWarning(f"Skipping ComfyUI path '{candidate}' (missing {missing_desc}).")

        error_msg = "Unable to locate a valid ComfyUI installation from ComfyUIPath entries. "
        error_msg += f"Tried: {', '.join(resolved_candidates)}"
        self.LogWarning(error_msg)
        self.FailRender(error_msg)
        return ""

    def Cleanup(self):
        """Clean up plugin resources"""
        self.thread_running = False
        
        # Clean up callbacks
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.PreRenderTasksCallback
        del self.PostRenderTasksCallback

        # Clean up stdout handlers
        for stdoutHandler in self.StdoutHandlers:
            del stdoutHandler.HandleCallback
    
    def InitializeProcess(self):
        """Initialize process settings"""
        self.SingleFramesOnly = False
        self.PluginType = PluginType.Simple 
        self.ProcessPriority = ProcessPriorityClass.BelowNormal
        self.UseProcessTree = True
        self.StdoutHandling = True
        self.PopupHandling = False

    def _get_cuda_device_arg(self) -> str:
        """
        Get CUDA device argument based on plugin configuration and worker settings.
        
        Returns:
            str: CUDA device argument string or empty string
        """
        assigned_gpu = None
        
        # 1. Check for specific CudaDeviceID from job plugin info
        cuda_device_id_plugin_info = self.GetPluginInfoEntryWithDefault("CudaDeviceID", "").strip()
        if cuda_device_id_plugin_info:
            try:
                int(cuda_device_id_plugin_info)  # Validate it's an integer string
                assigned_gpu = cuda_device_id_plugin_info
                self.LogInfo(f"Using specific CUDA device ID from plugin info: {assigned_gpu}")
            except ValueError:
                self.LogWarning(f"Invalid CudaDeviceID value '{cuda_device_id_plugin_info}' in plugin info. Ignoring.")

        # Endpoint-policy fallback resolves a stable UUID and validates it against
        # an explicit Deadline affinity constraint before using a CUDA ordinal.
        if assigned_gpu is None and self.endpoint_policy_active and not self.use_existing_comfyui:
            assigned_gpu = self._get_policy_launch_gpu()

        # 2. Check Deadline worker GPU affinity if not set by plugin info
        if assigned_gpu is None:
            assigned_gpu = self._get_gpu_from_worker_affinity()

        # 3. Use default device if still no assignment
        if assigned_gpu is None:
            assigned_gpu = self._get_default_cuda_device()

        return f"--cuda-device {assigned_gpu}" if assigned_gpu is not None else ""

    def _get_policy_launch_gpu(self) -> str:
        gpu_uuid = self.configured_launch_gpu_uuid
        if not gpu_uuid:
            raise ComfyUIError("Configured endpoint is unavailable and LaunchGpuUuid is missing; refusing CUDA 0 fallback.")
        try:
            output = subprocess.check_output(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], stderr=subprocess.STDOUT, universal_newlines=True, timeout=10)
        except Exception as e:
            raise ComfyUIError(f"Could not resolve LaunchGpuUuid with nvidia-smi: {e}")
        index = None
        for line in output.splitlines():
            fields = [value.strip() for value in line.split(",")]
            if len(fields) >= 2 and fields[1].lower() == gpu_uuid.lower():
                index = int(fields[0])
                break
        if index is None:
            raise ComfyUIError(f"LaunchGpuUuid '{gpu_uuid}' was not found on worker {self.GetSlaveName()}.")
        if self.OverrideGpuAffinity() and index not in list(self.GpuAffinity() or []):
            raise ComfyUIError(f"LaunchGpuUuid '{gpu_uuid}' resolved to CUDA {index}, outside Deadline affinity {list(self.GpuAffinity() or [])}.")
        self.LogInfo(f"Fallback launch GPU UUID {gpu_uuid} resolved to CUDA device {index}.")
        return str(index)

    def _get_gpu_from_worker_affinity(self) -> str:
        """Get GPU assignment from Deadline worker affinity settings"""
        if not self.OverrideGpuAffinity():
            self.LogInfo("Deadline worker GPU affinity is not overridden for this worker.")
            return None

        available_gpus = self.GpuAffinity()
        if not available_gpus:
            self.LogInfo("Worker GPU affinity is overridden but no specific GPUs are assigned.")
            return None

        self.LogInfo(f"Worker has GPU affinity set by Deadline: {available_gpus}")
        selected_gpu_device_id = available_gpus[self.GetThreadNumber() % len(available_gpus)]
        assigned_gpu = str(selected_gpu_device_id)
        self.LogInfo(f"Assigning CUDA device based on worker affinity: {assigned_gpu}")
        return assigned_gpu

    def _get_default_cuda_device(self) -> str:
        """Get default CUDA device if configured"""
        use_default_device_zero = self.GetBooleanPluginInfoEntryWithDefault("DefaultCudaDeviceZero", True)
        if use_default_device_zero:
            self.LogInfo("No CUDA device assigned. Defaulting to CUDA device 0.")
            return "0"
        else:
            self.LogInfo("No CUDA device assigned and DefaultCudaDeviceZero is false. ComfyUI will use default GPU behavior.")
            return None

    def _setup_batch_processing(self):
        """Setup batch processing configuration"""
        self.batch_mode = self.GetBooleanPluginInfoEntryWithDefault("BatchMode", False)
        self.batch_count = int(self.GetPluginInfoEntryWithDefault("BatchCount", "1"))
        if self.batch_mode:
            self.chunk_size = int(self.GetJob().ChunkSize)
            self.assigned_variation_indices = self._get_assigned_variation_indices()
            self.chunk_size = len(self.assigned_variation_indices)
            self.LogInfo(f"Batch mode enabled. Assigned variation indices: {self.assigned_variation_indices}")
        else:
            self.chunk_size = 1
            self.assigned_variation_indices = [0]
            self.LogInfo("Batch mode disabled. Processing single task.")
        
        self.prompts_executed = 0

    def _get_assigned_variation_indices(self):
        """Return Deadline frame numbers for the current task; frames are variation indices."""
        chunk_size = max(1, int(self.GetJob().ChunkSize))
        task_id = int(self.GetCurrentTaskId())
        start_from_task = task_id * chunk_size
        end_from_task = min(start_from_task + chunk_size - 1, self.batch_count - 1)
        indices_from_task = list(range(start_from_task, end_from_task + 1))

        try:
            start_frame = int(self.GetStartFrame())
            end_frame = int(self.GetEndFrame())
            indices = list(range(start_frame, end_frame + 1))
        except Exception as e:
            self.LogWarning(f"Could not read Deadline task frame range, using task/chunk math: {e}")
            indices = indices_from_task

        if len(indices) != len(indices_from_task):
            self.LogWarning(
                f"Deadline frame range reported {indices}, but task/chunk math expects "
                f"{indices_from_task}; using task/chunk math for variation assignment."
            )
            indices = indices_from_task

        return [index for index in indices if 0 <= index < self.batch_count] or [0]

    def _setup_output_directory(self):
        """Setup output directory configuration"""
        job_output_directory_plugin = self.GetPluginInfoEntryWithDefault("JobOutputDirectory", "")
        
        if job_output_directory_plugin:
            self._setup_custom_output_directory(job_output_directory_plugin)
        else:
            self._setup_default_output_directory()

    def _setup_custom_output_directory(self, output_dir: str):
        """Setup custom output directory"""
        self.comfyui_output_dir = os.path.abspath(output_dir)
        self.custom_output_dir_specified = True
        self.LogInfo(f"ComfyUI will output directly to user-specified directory: {self.comfyui_output_dir}")
        
        if not os.path.exists(self.comfyui_output_dir):
            self._create_directory(self.comfyui_output_dir, "user-specified output")

    def _setup_default_output_directory(self):
        """Setup default ComfyUI output directory"""
        # Check for configured default output directory first
        default_output_dir = self.GetConfigEntryWithDefault("DefaultOutputDirectory", "")
        
        if default_output_dir:
            self.comfyui_output_dir = os.path.abspath(default_output_dir)
            self.custom_output_dir_specified = False
            self.LogInfo(f"Using configured default output directory: {self.comfyui_output_dir}")
        else:
            # Fall back to ComfyUI's standard output directory
            comfyui_path = self._resolve_comfyui_install_path()
            if not comfyui_path:
                raise ComfyUIError("ComfyUI installation path could not be resolved for output directory setup.")
            self.comfyui_output_dir = os.path.join(comfyui_path, "ComfyUI", "output")
            self.custom_output_dir_specified = False
            self.LogInfo(f"Using ComfyUI's default output directory: {self.comfyui_output_dir}")
        
        if not os.path.exists(self.comfyui_output_dir):
            self._create_directory(self.comfyui_output_dir, "default output")

    def _setup_input_directory(self):
        """Setup optional staged ComfyUI input directory for default loader nodes."""
        input_dir = self.GetPluginInfoEntryWithDefault("JobInputDirectory", "").strip()
        self.input_manifest_file = self.GetPluginInfoEntryWithDefault("InputManifestFile", "").strip()
        self.submission_id = self.GetPluginInfoEntryWithDefault("SubmissionId", "").strip()

        if not input_dir:
            self.comfyui_input_dir = ""
            self.LogInfo("No staged input directory specified; ComfyUI will use its default input folder.")
            return

        try:
            input_dir = RepositoryUtils.CheckPathMapping(input_dir)
        except Exception as e:
            self.LogWarning(f"Path mapping failed for JobInputDirectory '{input_dir}': {e}")

        self.comfyui_input_dir = os.path.abspath(os.path.expandvars(input_dir))
        if not os.path.isdir(self.comfyui_input_dir):
            raise ComfyUIError(f"Staged input directory does not exist: {self.comfyui_input_dir}")

        self.LogInfo(f"ComfyUI will use staged input directory: {self.comfyui_input_dir}")

    def _create_directory(self, directory_path: str, description: str):
        """Create a directory with error handling"""
        try:
            os.makedirs(directory_path)
            self.LogInfo(f"Created {description} directory: {directory_path}")
        except Exception as e:
            self.LogWarning(f"Could not create {description} directory {directory_path}: {e}")

    def _calculate_comfyui_port(self) -> str:
        """Calculate the port for ComfyUI based on CUDA device"""
        if self.endpoint_policy_active:
            return self._configure_policy_endpoint()
        cuda_arg = self._get_cuda_device_arg()
        cuda_device_id = None
        
        if cuda_arg:
            cuda_match = re.search(r'--cuda-device\s+(\d+)', cuda_arg)
            if cuda_match:
                cuda_device_id = int(cuda_match.group(1))
                self.LogInfo(f"Using CUDA device ID {cuda_device_id} for port calculation")
        
        default_port = int(self.GetPluginInfoEntryWithDefault("ComfyUIPort", str(DEFAULT_PORT)))
        if cuda_device_id is not None:
            base_port = default_port + (cuda_device_id * PORT_OFFSET_PER_GPU)
            self.LogInfo(f"Calculated base port {base_port} for CUDA device {cuda_device_id}")
        else:
            base_port = default_port
            self.LogInfo(f"No CUDA device ID available, using default port {base_port}")
        
        return self._determine_final_port(base_port)

    def _load_worker_endpoint_policy(self):
        worker = self.GetSlaveName()
        try:
            settings = RepositoryUtils.GetSlaveSettings(worker, True)
            endpoint = settings.GetSlaveExtraInfoKeyValueWithDefault("ComfyUIApiUrl", "").strip().rstrip("/")
            gpu_uuid = settings.GetSlaveExtraInfoKeyValueWithDefault("ComfyUILaunchGpuUuid", "").strip()
        except Exception as e:
            self.LogWarning(f"Could not read ComfyUI worker endpoint settings for {worker}: {e}")
            endpoint, gpu_uuid = "", ""
        if endpoint:
            parsed = urllib.parse.urlparse(endpoint)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ComfyUIError(f"Worker {worker} needs a local http://127.0.0.1:<port> ComfyUIApiUrl.")
            self.endpoint_policy_active = True
            self.configured_comfyui_api_url = endpoint
            self.configured_launch_gpu_uuid = gpu_uuid
            self.LogInfo(f"Using per-worker ComfyUI endpoint policy from Deadline worker settings: {endpoint}")
            return
        raw = self.GetConfigEntryWithDefault("ComfyUIWorkerEndpoints", "").strip()
        if not raw:
            return
        try:
            policies = json.loads(raw)
            entry = next((v for k, v in policies.items() if str(k).lower() == str(worker).lower()), None)
        except Exception as e:
            raise ComfyUIError(f"ComfyUIWorkerEndpoints must be valid JSON: {e}")
        if entry is None:
            return
        if not isinstance(entry, dict):
            raise ComfyUIError(f"Worker {worker} endpoint policy must be an object.")
        endpoint = str(entry.get("ComfyUIApiUrl", "")).strip().rstrip("/")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ComfyUIError(f"Worker {worker} needs a local http://127.0.0.1:<port> ComfyUIApiUrl.")
        self.endpoint_policy_active = True
        self.configured_comfyui_api_url = endpoint
        self.configured_launch_gpu_uuid = str(entry.get("LaunchGpuUuid", "")).strip()

    def _verified_comfy_endpoint(self, endpoint):
        try:
            prompt = self.http_request(endpoint + "/prompt", verbose=False)
            stats = self.http_request(endpoint + "/system_stats", verbose=False)
            identity = self.http_request(endpoint + "/deadline/session", verbose=False)
            data = stats["json"]()
            identity_data = identity["json"]() if identity["status_code"] == 200 else {}
            expected_root = os.path.normcase(os.path.realpath(os.path.join(self.comfyui_install_path, "ComfyUI")))
            actual_root = os.path.normcase(os.path.realpath(str(identity_data.get("comfyui_root", ""))))
            if not identity_data:
                identity_data = self._local_port_process_identity(urllib.parse.urlparse(endpoint).port)
                actual_root = expected_root if identity_data else ""
            if (prompt["status_code"] != 200 or stats["status_code"] != 200 or
                    not isinstance(data, dict) or "devices" not in data or
                    identity_data.get("product") != "ComfyUI-Deadline-Plugin" or
                    identity_data.get("protocol") != 1 or
                    not identity_data.get("session_id") or not identity_data.get("pid") or
                    actual_root != expected_root):
                return False
            self.endpoint_session_id = str(identity_data["session_id"])
            argv = data.get("system", {}).get("argv", [])
            if "--input-directory" in argv:
                position = argv.index("--input-directory") + 1
                if position >= len(argv) or not os.path.isabs(argv[position]):
                    raise ComfyUIError("Existing endpoint has an unsupported input-directory argument.")
                self.reuse_gui_input_root = argv[position]
            else:
                self.reuse_gui_input_root = os.path.join(self.comfyui_install_path, "ComfyUI", "input")
            if "--output-directory" in argv:
                position = argv.index("--output-directory") + 1
                if position >= len(argv) or not os.path.isabs(argv[position]):
                    raise ComfyUIError("Existing endpoint has an unsupported output-directory argument.")
                self.reuse_gui_output_root = argv[position]
            else:
                self.reuse_gui_output_root = os.path.join(self.comfyui_install_path, "ComfyUI", "output")
            self.LogInfo(
                f"Verified ComfyUI endpoint ownership: pid={identity_data['pid']}, "
                f"session={self.endpoint_session_id}, root={actual_root}"
            )
            return True
        except Exception as e:
            self.LogInfo(f"Configured ComfyUI endpoint {endpoint} unavailable: {e}")
            return False

    def _local_port_process_identity(self, port):
        """Verify a legacy endpoint by the OS port owner and exact install Python."""
        if platform.system().lower() != "windows":
            return {}
        try:
            netstat = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"],
                stderr=subprocess.STDOUT, universal_newlines=True, timeout=10,
            )
            pid = None
            for line in netstat.splitlines():
                columns = line.split()
                if len(columns) < 5 or columns[-2].upper() != "LISTENING":
                    continue
                local_address = columns[1].rsplit(":", 1)
                if len(local_address) == 2 and local_address[1] == str(port):
                    pid = int(columns[-1])
                    break
            if not pid:
                return {}
            ps_command = (
                f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | "
                "Select-Object ProcessId,ExecutablePath,CommandLine | ConvertTo-Json -Compress"
            )
            process_json = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_command],
                stderr=subprocess.STDOUT, universal_newlines=True, timeout=15,
            ).strip()
            process_data = json.loads(process_json)
            expected_python = os.path.normcase(os.path.realpath(
                os.path.join(self.comfyui_install_path, "python_embeded", "python.exe")
            ))
            actual_python = os.path.normcase(os.path.realpath(process_data.get("ExecutablePath", "")))
            command_line = str(process_data.get("CommandLine", "")).lower().replace("\\", "/")
            if actual_python != expected_python or "comfyui/main.py" not in command_line:
                return {}
            return {
                "product": "ComfyUI-Deadline-Plugin",
                "protocol": 1,
                "session_id": f"legacy-pid-{pid}",
                "pid": pid,
                "comfyui_root": os.path.join(self.comfyui_install_path, "ComfyUI"),
            }
        except Exception as e:
            self.LogInfo(f"Could not verify legacy endpoint port owner on {port}: {e}")
            return {}

    def _configure_policy_endpoint(self):
        endpoint = self.configured_comfyui_api_url
        self.use_existing_comfyui = False
        self.reuse_gui_output_root = ""
        self.reuse_gui_input_root = ""
        port = urllib.parse.urlparse(endpoint).port
        self.comfyui_api_url, self.comfyui_port = endpoint, str(port)
        if self._verified_comfy_endpoint(endpoint):
            self.use_existing_comfyui, self.server_started = True, True
            self.LogInfo(f"Reusing verified ComfyUI endpoint {endpoint}; its running session determines GPU and this task queues only its own prompts.")
            return self.comfyui_port
        if self._is_port_in_use(port):
            raise ComfyUIError(f"Configured endpoint port {port} is occupied by an unverified service; refusing another port or replacement.")
        self.LogInfo(f"Configured endpoint unavailable; fallback will start only at {endpoint}.")
        return self.comfyui_port

    def _determine_final_port(self, base_port: int) -> str:
        """Determine final port to use, checking for existing instances"""
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)

        self.use_existing_comfyui = False
        if worker_mode or distributed_mode:
            worker_port = self._calculate_worker_port(base_port)
            self.comfyui_port = self._find_available_port(worker_port)
            self.LogInfo(f"Worker/distributed mode: starting isolated ComfyUI on port {self.comfyui_port}")
        else:
            self.comfyui_port = self._find_available_port(base_port)
            self.LogInfo(f"Normal render mode: starting isolated ComfyUI on port {self.comfyui_port}")

        self.comfyui_api_url = f"http://127.0.0.1:{self.comfyui_port}"
        
        return self.comfyui_port

    def _calculate_worker_port(self, base_port: int) -> int:
        """Calculate unique worker port based on task ID to avoid conflicts"""
        try:
            # Get task ID from Deadline environment
            task_id = int(os.environ.get('DEADLINE_TASK_ID', '1'))
            
            # Calculate unique worker port: base_port + 100 + task_id
            # This ensures workers get ports like 8289, 8290, 8291, etc.
            # For single PC testing, this allows multiple workers on one GPU
            worker_port = base_port + 100 + task_id
            
            self.LogInfo(f"Calculated worker port: {worker_port} (base: {base_port}, task: {task_id})")
            return worker_port
        except ValueError:
            # Fallback if task ID is not a valid integer
            fallback_port = base_port + 100
            self.LogInfo(f"Could not parse task ID, using fallback port: {fallback_port}")
            return fallback_port

    def PreRenderTasks(self):
        """Setup tasks before rendering"""
        self.LogInfo("ComfyUI PreRenderTasks started.")
        
        try:
            self._setup_batch_processing()
            self._load_worker_endpoint_policy()
            comfyui_path = self._resolve_comfyui_install_path()
            if not comfyui_path:
                raise ComfyUIError("ComfyUI installation path could not be resolved.")
            self._setup_output_directory()
            self._setup_input_directory()
            self._setup_temp_directory()
            self._calculate_comfyui_port()
            self.task_completed = False
            self.LogInfo("PreRenderTasks completed successfully.")
        except Exception as e:
            self.LogWarning(f"Error in PreRenderTasks: {e}")
            raise ComfyUIError(f"PreRenderTasks failed: {str(e)}")

    def _setup_temp_directory(self):
        """Create temporary directory for job files"""
        self.temp_dir = self.CreateTempDirectory("comfyui_job")

    def _is_port_in_use(self, port: int) -> bool:
        """Check if the given port is already in use"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex(('127.0.0.1', port))
                return result == 0
        except:
            return False
    
    def _find_available_port(self, start_port: int) -> str:
        """Find an available port starting from start_port"""
        port = start_port
        max_port = start_port + MAX_PORT_SEARCH_RANGE
        
        while port < max_port:
            if not self._is_port_in_use(port):
                return str(port)
            port += 1
        
        return str(start_port)

    def PostRenderTasks(self):
        """Cleanup tasks after rendering"""
        self.LogInfo("ComfyUI PostRenderTasks started.")

        if not self.task_completed:
            message = self.submission_error or (
                f"ComfyUI process ended before this Deadline task completed on worker {self.GetSlaveName()}; "
                f"server_started={self.server_started}, workflow_submitted={self.workflow_submitted}, "
                f"completed_prompts={self.prompts_executed}/{self.chunk_size}."
            )
            self.LogWarning(message)
            self.FailRender(message)
            return
        
        # Wait for files to be written
        self.LogInfo("Waiting for files to be written to disk...")
        time.sleep(FILE_WRITE_DELAY)
        
        self._log_output_directory_status()
        self.LogInfo("PostRenderTasks finished.")

    def _log_output_directory_status(self):
        """Log the status of output directories"""
        if self.custom_output_dir_specified:
            self._log_custom_directory_status()
        else:
            self._log_default_directory_status()

    def _log_custom_directory_status(self):
        """Log status of custom output directory"""
        self.LogInfo(f"ComfyUI was instructed to output to: {self.comfyui_output_dir}")
        if os.path.exists(self.comfyui_output_dir):
            try:
                final_outputs = os.listdir(self.comfyui_output_dir)
                self.LogInfo(f"Output directory contains {len(final_outputs)} item(s). Examples: {final_outputs[:5]}")
            except Exception as e:
                self.LogWarning(f"Could not list contents of output directory: {e}")
        else:
            self.LogWarning(f"Output directory was not found: {self.comfyui_output_dir}")

    def _log_default_directory_status(self):
        """Log status of default output directory"""
        self.LogInfo(f"ComfyUI used default output directory: {self.comfyui_output_dir}")
        if os.path.exists(self.comfyui_output_dir):
            try:
                default_outputs = os.listdir(self.comfyui_output_dir)
                self.LogInfo(f"Default output directory contains {len(default_outputs)} item(s). Examples: {default_outputs[:5]}")
            except Exception as e:
                self.LogWarning(f"Could not list contents of default output directory: {e}")
        else:
            self.LogWarning(f"Default output directory was not found: {self.comfyui_output_dir}")

    def RenderExecutable(self):
        """Get the Python executable for ComfyUI"""
        comfyui_path = self._resolve_comfyui_install_path()
        if not comfyui_path:
            return ""
        python_exe = os.path.join(comfyui_path, "python_embeded", "python.exe")
        
        if os.path.exists(python_exe):
            self.LogInfo(f"Using ComfyUI embedded Python: {python_exe}")
            # Set the Deadline worker name as environment variable for ComfyUI process
            self._set_deadline_environment_variables()
            return python_exe
        else:
            error_msg = f"ComfyUI embedded Python not found at: {python_exe}. Please check your ComfyUIPath entries."
            self.LogWarning(error_msg)
            self.FailRender(error_msg)
            return ""

    def RenderArgument(self):
        """Build command line arguments for ComfyUI"""
        comfyui_path = self._resolve_comfyui_install_path()
        if not comfyui_path:
            return ""
        comfyui_main_py = os.path.join(comfyui_path, "ComfyUI", "main.py")
        
        # Validate that main.py exists
        if not os.path.exists(comfyui_main_py):
            error_msg = f"ComfyUI main.py not found at: {comfyui_main_py}. Please check your ComfyUIPath configuration."
            self.LogWarning(error_msg)
            self.FailRender(error_msg)
            return ""
        
        # If using existing ComfyUI instance, return dummy command
        if self.use_existing_comfyui:
            return self._create_dummy_command()
        
        # Build arguments for new ComfyUI instance
        return self._build_comfyui_arguments(comfyui_main_py)

    def _create_dummy_command(self) -> str:
        """Create dummy command for existing ComfyUI instances"""
        self.reuse_completion_marker = os.path.join(self.temp_dir, "comfyui_reuse_completion.json")
        self.LogInfo("Using existing ComfyUI instance - starting own-prompt waiter.")
        
        # Start workflow submission thread
        workflow_thread = threading.Thread(target=self.submit_workflow)
        workflow_thread.daemon = True
        workflow_thread.start()
        
        dummy_script = self._get_dummy_script()
        encoded = base64.b64encode(dummy_script.encode("utf-8")).decode("ascii")
        return f'-c "import base64;exec(base64.b64decode(\'{encoded}\'))"'

    def _get_dummy_script(self) -> str:
        """Wait for this task's completion marker; Deadline cancellation remains authoritative."""
        return """import json, os, sys, time
marker = %r
while not os.path.exists(marker):
    time.sleep(1)
with open(marker, 'r') as handle:
    result = json.load(handle)
os.remove(marker)
sys.exit(0 if result.get('success') else 1)
""" % self.reuse_completion_marker

    def _signal_reuse_waiter(self, success):
        if self.reuse_completion_marker:
            temp_marker = self.reuse_completion_marker + ".tmp"
            with open(temp_marker, "w") as handle:
                json.dump({"success": bool(success)}, handle)
            os.replace(temp_marker, self.reuse_completion_marker)

    def _build_comfyui_arguments(self, comfyui_main_py: str) -> str:
        """Build command line arguments for new ComfyUI instance"""
        port = getattr(self, "comfyui_port", str(DEFAULT_PORT))
        args_list = []

        # Add Python flags
        if self.GetBooleanPluginInfoEntryWithDefault("PythonNoUserSite", True):
            args_list.append("-s")

        # Add main script and port
        args_list.append(f'"{comfyui_main_py}"')
        args_list.append(f"--port {port}")

        # Add CUDA device argument
        cuda_arg = self._get_cuda_device_arg()
        if cuda_arg:
            args_list.append(cuda_arg)
        
        # Add ComfyUI flags for worker mode
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
        
        if worker_mode or distributed_mode:
            args_list.append("--listen")  # Allow external connections
            args_list.append("--enable-cors-header")  # Enable CORS for API access
            ##args_list.append("--dont-print-server")  # Reduce startup output for workers
            
        # Always add windows standalone build flag if on Windows
        if platform.system().lower() == 'windows':
            args_list.append("--windows-standalone-build")


        args_list.append("--disable-auto-launch")
        if self.GetBooleanPluginInfoEntryWithDefault("DisableDynamicVRAM", False):
            args_list.append("--disable-dynamic-vram")
        reserve_vram = float(self.GetPluginInfoEntryWithDefault("ReserveVRAM", "0"))
        if not 0 <= reserve_vram <= 64:
            raise ValueError("ReserveVRAM must be between 0 and 64 GB")
        if reserve_vram > 0:
            args_list.append(f"--reserve-vram {reserve_vram:g}")

        # Add output directory if custom one was specified
        if self.custom_output_dir_specified and self.comfyui_output_dir:
            args_list.append(f'--output-directory "{self.comfyui_output_dir}"')
            self.LogInfo(f"Passing --output-directory \"{self.comfyui_output_dir}\" to ComfyUI.")
        else:
            self.LogInfo("Not passing --output-directory to ComfyUI, it will use its default.")

        if self.comfyui_input_dir:
            args_list.append(f'--input-directory "{self.comfyui_input_dir}"')
            self.LogInfo(f"Passing --input-directory \"{self.comfyui_input_dir}\" to ComfyUI.")

        args = " ".join(args_list)
        self.LogInfo(f"Render Arguments: {args}")
        return args

    def HandleServerStarted(self):
        """Called when the ComfyUI server has started"""
        # Prevent multiple triggers from stdout handlers
        if self.workflow_submitted:
            self.LogInfo("ComfyUI server startup detected, but workflow already submitted - ignoring")
            return
            
        self.LogInfo("ComfyUI server has started")
        self.server_started = True
        
        # Check if we're in worker/distributed mode
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
        
        self.LogInfo(f"Distributed config check - WorkerMode: {worker_mode}, DistributedMode: {distributed_mode}")
        self.LogInfo(f"use_existing_comfyui={self.use_existing_comfyui}, workflow_submitted={self.workflow_submitted}")
        
        # Start workflow submission if not using existing instance
        if not self.use_existing_comfyui and not self.workflow_submitted:
            # Mark as submitted IMMEDIATELY to prevent race conditions
            self.workflow_submitted = True
            self.LogInfo("Starting workflow submission thread...")
            workflow_thread = threading.Thread(target=self.submit_workflow)
            workflow_thread.daemon = True
            workflow_thread.start()
        else:
            self.LogInfo("Skipping workflow submission - using existing instance or already submitted")

    def http_request(self, url: str, method: str = "GET", data=None, headers=None, verbose: bool = True) -> dict:
        """Make an HTTP request to the ComfyUI API"""
        if not self.thread_running:
            self.LogInfo("Thread stopping due to task completion")
            return {'status_code': 0, 'text': '', 'json': lambda: {}}
            
        if verbose:
            self.LogInfo(f"Making {method} request to {url}")
        
        if headers is None:
            headers = {}
            
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = response.read().decode('utf-8')
                return {
                    'status_code': response.status,
                    'text': response_data,
                    'json': lambda: json.loads(response_data) if response_data else {}
                }
        except urllib.error.HTTPError as e:
            self.LogWarning(f"HTTP Error: {e.code} {e.reason}")
            return {
                'status_code': e.code,
                'text': e.read().decode('utf-8'),
                'json': lambda: {}
            }
        except Exception as e:
            self.LogWarning(f"Error in HTTP request: {str(e)}")
            raise
    
    def modify_workflow_seeds(self, workflow_data: dict, task_id: int) -> bool:
        """
        Modify seeds in the workflow based on task ID and seed mode.
        
        Args:
            workflow_data: The workflow data
            task_id: Current task ID (or frame number)
            
        Returns:
            bool: True if seeds were modified, False otherwise
        """
        seed_mode = self.GetPluginInfoEntryWithDefault("SeedMode", "fixed")
        
        if seed_mode == "fixed":
            self.LogInfo(f"No seed manipulation: SeedMode is 'fixed' (keep)")
            return False
        
        self.LogInfo(f"Seed Control: Using '{seed_mode}' mode for task ID {task_id}")
        seeds_modified = False
        
        for node_id, node in workflow_data.items():
            if not isinstance(node, dict) or "inputs" not in node:
                continue
            
            seeds_modified |= self._modify_node_seeds(node, node_id, task_id, seed_mode)
        
        if not seeds_modified:
            self.LogInfo(f"Seed Control: No seed parameters found in workflow that could be modified")
        
        return seeds_modified

    def _modify_node_seeds(self, node: dict, node_id: str, task_id: int, seed_mode: str) -> bool:
        """Modify seeds in a single node"""
        inputs = node.get("inputs", {})
        if not inputs:
            return False
        
        node_type = node.get("class_type", "unknown")
        seeds_modified = False
        
        for param_name in SEED_PARAMETER_NAMES:
            if param_name in inputs:
                try:
                    original_seed = int(inputs[param_name])
                    new_seed = self._calculate_new_seed(original_seed, task_id, seed_mode, inputs)
                    
                    if new_seed != original_seed:
                        node["inputs"][param_name] = new_seed
                        self.LogInfo(f"Seed Control: Modified node {node_id} ({node_type}) seed from {original_seed} to {new_seed}")
                        seeds_modified = True
                    else:
                        self.LogInfo(f"Seed Control: Node {node_id} ({node_type}) seed kept at {original_seed}")
                        
                except (ValueError, TypeError):
                    self.LogInfo(f"Seed Control: Node {node_id} ({node_type}) has non-numeric {param_name}: {inputs[param_name]}")
        
        return seeds_modified

    def _calculate_new_seed(self, original_seed: int, task_id: int, seed_mode: str, inputs: dict) -> int:
        """Calculate new seed based on mode and parameters"""
        if seed_mode == "auto":
            control_mode = inputs.get("control_after_generate", "increment")
            if control_mode == "fixed":
                return original_seed
            elif control_mode == "increment":
                return original_seed + task_id
            elif control_mode == "decrement":
                return max(0, original_seed - task_id)
            elif control_mode == "randomize":
                return random.randint(0, MAX_SEED_VALUE)
            else:
                return original_seed + task_id
        elif seed_mode == "change":
            return random.randint(0, MAX_SEED_VALUE)
        else:
            return random.randint(0, MAX_SEED_VALUE)

    def _set_deadline_environment_variables(self):
        """Set Deadline-specific environment variables for ComfyUI process"""
        try:
            os.environ['GIT_PYTHON_REFRESH'] = 'quiet'
            self.LogInfo("Set GIT_PYTHON_REFRESH=quiet so workers do not require git.exe for ComfyUI-Manager startup")

            # Get the actual Deadline worker name and set it as environment variable
            slave_name = self.GetSlaveName()
            if slave_name:
                os.environ['DEADLINE_SLAVE_NAME'] = slave_name
                self.LogInfo(f"Set DEADLINE_SLAVE_NAME environment variable: {slave_name}")
            else:
                self.LogWarning("Could not get Deadline worker name (GetSlaveName returned None)")
                
            # Also set other useful Deadline variables
            try:
                job = self.GetJob()
                if job:
                    os.environ['DEADLINE_JOB_ID'] = job.JobId
                    self.LogInfo(f"Set DEADLINE_JOB_ID environment variable: {job.JobId}")
            except:
                self.LogWarning("Could not get job ID for environment variable")
                
            try:
                task_id = self.GetCurrentTaskId()
                if task_id is not None:
                    os.environ['DEADLINE_TASK_ID'] = str(task_id)
                    self.LogInfo(f"Set DEADLINE_TASK_ID environment variable: {task_id}")
            except:
                self.LogWarning("Could not get task ID for environment variable")
                
        except Exception as e:
            self.LogWarning(f"Error setting Deadline environment variables: {e}")

    def inject_deadline_seed_parameters(self, workflow_data: dict) -> bool:
        """
        Inject task_id and batch_mode into Deadline seed nodes.
        
        Args:
            workflow_data: The workflow data
            
        Returns:
            bool: True if any nodes were modified, False otherwise
        """
        try:
            task_id = int(self.GetCurrentTaskId())
            batch_mode = self.batch_mode
            nodes_modified = False
            
            for node_id, node in workflow_data.items():
                if not isinstance(node, dict):
                    continue
                    
                node_type = node.get("class_type")
                if node_type in DEADLINE_SEED_NODE_TYPES:
                    if "inputs" not in node:
                        node["inputs"] = {}
                    
                    # Inject task_id and batch_mode as hidden parameters
                    node["inputs"]["task_id"] = task_id
                    node["inputs"]["batch_mode"] = batch_mode
                    
                    self.LogInfo(f"Injected task_id={task_id}, batch_mode={batch_mode} into {node_type} node {node_id}")
                    nodes_modified = True
            
            return nodes_modified
            
        except Exception as e:
            self.LogWarning(f"Error injecting deadline seed parameters: {e}")
            return False

    def load_and_validate_workflow(self) -> dict:
        """Load workflow file and validate its structure"""
        workflow_file = self._get_workflow_file_path()
        if not workflow_file or not os.path.exists(workflow_file):
            self.LogWarning(f"Workflow file does not exist at: {workflow_file}")
            raise ComfyUIError(f"Workflow file not found: {workflow_file}")
            return None
        
        try:
            workflow_data = self._load_workflow_from_file(workflow_file)
            workflow_data = self.validate_workflow(workflow_data)

            worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
            if worker_mode or distributed_mode:
                deadline_seeds_injected = self.inject_deadline_seed_parameters(workflow_data)
                if not deadline_seeds_injected:
                    task_id = self.GetCurrentTaskId()
                    seeds_modified = self.modify_workflow_seeds(workflow_data, task_id)
                    if seeds_modified:
                        self.LogInfo(f"Applied legacy seed manipulation for distributed task ID {task_id}")
                else:
                    self.LogInfo("Deadline seed nodes detected for distributed worker workflow")
            else:
                self.LogInfo("Normal V2 job: seed variation will be applied per queued prompt via Deadline seed nodes only")
            
            return workflow_data
        except Exception as e:
            self.LogWarning(f"Error loading or validating workflow file '{workflow_file}': {e}")
            raise ComfyUIError(f"Error loading or validating workflow file: {str(e)}")
            return None

    def _get_workflow_file_path(self) -> str:
        """Get the workflow file path from plugin settings"""
        # Check if we're in distributed/worker mode
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
        
        if distributed_mode or worker_mode:
            # For distributed workers, use the WorkflowFile from plugin info (should be dummy workflow)
            workflow_file = self.GetPluginInfoEntryWithDefault("WorkflowFile", "")
            if not workflow_file:
                # Fallback to ComfyWorkflowFile if WorkflowFile is not set
                workflow_file = self.GetPluginInfoEntryWithDefault("ComfyWorkflowFile", self.GetDataFilename())
        else:
            # Normal V2 jobs execute the API prompt, while the first auxiliary file may be the standard UI workflow.
            workflow_file = self.GetPluginInfoEntryWithDefault("ComfyWorkflowFile", "")
            if not workflow_file:
                workflow_file = self.GetDataFilename()
        
        workflow_file = self._resolve_workflow_file_path(workflow_file)
        self.LogInfo(f"Workflow file setting from plugin info: '{workflow_file}'")
        return workflow_file

    def _resolve_workflow_file_path(self, workflow_file: str) -> str:
        """Resolve absolute or auxiliary-file-relative workflow paths."""
        workflow_file = (workflow_file or "").strip().strip('"')
        if not workflow_file:
            return ""

        try:
            mapped = RepositoryUtils.CheckPathMapping(workflow_file)
        except Exception:
            mapped = workflow_file

        if os.path.isabs(mapped):
            return mapped

        candidates = []
        data_filename = self.GetDataFilename()
        if data_filename:
            candidates.append(os.path.join(os.path.dirname(data_filename), mapped))

        try:
            candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), mapped))
        except Exception:
            pass

        if self.temp_dir:
            candidates.append(os.path.join(self.temp_dir, mapped))

        candidates.append(os.path.abspath(mapped))

        for candidate in candidates:
            try:
                candidate = RepositoryUtils.CheckPathMapping(candidate)
            except Exception:
                pass
            if os.path.exists(candidate):
                return candidate

        self.LogWarning(f"Could not resolve relative workflow file '{workflow_file}'. Tried: {candidates}")
        return candidates[0] if candidates else mapped

    def _load_standard_workflow_metadata(self):
        """Load the optional UI workflow used for image metadata drag-and-drop."""
        standard_workflow_file = self.GetPluginInfoEntryWithDefault("StandardWorkflowFile", "").strip()
        if not standard_workflow_file:
            self.standard_workflow = None
            return

        try:
            standard_workflow_file = self._resolve_workflow_file_path(standard_workflow_file)
            if not os.path.exists(standard_workflow_file):
                self.LogWarning(f"Standard workflow metadata file not found: {standard_workflow_file}")
                self.standard_workflow = None
                return

            with open(standard_workflow_file, "r") as f:
                self.standard_workflow = json.load(f)
            self.standard_workflow_file = standard_workflow_file
            self.LogInfo(f"Loaded standard workflow metadata: {standard_workflow_file}")
        except Exception as e:
            self.standard_workflow = None
            self.LogWarning(f"Could not load standard workflow metadata: {e}")

    def _load_workflow_from_file(self, workflow_file: str) -> dict:
        """Load workflow data from JSON file"""
        with open(workflow_file, 'r') as f:
            workflow_content = f.read()
            workflow_data = json.loads(workflow_content)
        self.LogInfo(f"Successfully loaded workflow file: {workflow_file}")
        return workflow_data
    
    def validate_workflow(self, workflow_data: dict) -> dict:
        """Check workflow for output nodes and convert to UI format if needed"""
        has_save_image, has_output_node = self._check_workflow_output_nodes(workflow_data)
        
        if not has_output_node:
            self.LogWarning("No output nodes found in workflow. You need at least one SaveImage, PreviewImage, or SaveVideo node.")
            
        if not has_save_image:
            self.LogWarning("No SaveImage node found in workflow. Images may not be saved to disk.")
            
        # Convert from API format to UI format if needed
        if "nodes" in workflow_data:
            self.LogInfo("Converting from API format to UI format...")
            workflow_data = self._convert_api_to_ui_format(workflow_data)
            
        return workflow_data

    def _check_workflow_output_nodes(self, workflow_data: dict) -> tuple:
        """Check for output nodes in workflow"""
        has_save_image = False
        has_output_node = False
        
        # Check UI format (numbered keys)
        for key, node in workflow_data.items():
            if isinstance(node, dict):
                class_type = node.get("class_type", "")
                if class_type == "SaveImage":
                    has_save_image = True
                    has_output_node = True
                    self.LogInfo(f"Found SaveImage node in workflow")
                elif class_type in OUTPUT_NODE_TYPES:
                    has_output_node = True
                    self.LogInfo(f"Found output node {class_type} in workflow")
        
        # Check API format (nodes array)
        if not has_output_node and "nodes" in workflow_data:
            for node in workflow_data["nodes"]:
                if isinstance(node, dict):
                    class_type = node.get("class_type", "")
                    if class_type == "SaveImage":
                        has_save_image = True
                        has_output_node = True
                        self.LogInfo(f"Found SaveImage node in workflow")
                    elif class_type in OUTPUT_NODE_TYPES:
                        has_output_node = True
                        self.LogInfo(f"Found output node {class_type} in workflow")
        
        return has_save_image, has_output_node

    def _convert_api_to_ui_format(self, workflow_data: dict) -> dict:
        """Convert workflow from API format to UI format"""
        converted_workflow = {}
        for node in workflow_data["nodes"]:
            node_id = str(node.get("id", 0))
            converted_workflow[node_id] = node
        return converted_workflow

    def HandleStdoutProgressBar(self):
        """Handle progress in the format '  4%|4         | 1/25 [00:02<00:59,  2.50s/it]'"""
        try:
            percent = float(self.GetRegexMatch(1))
            current_step = int(self.GetRegexMatch(2))
            total_steps = int(self.GetRegexMatch(3))
            
            # Calculate overall chunk progress if needed
            if self.chunk_size > 1:
                completed_progress = (self.prompts_executed / self.chunk_size) * 100
                current_contribution = (percent / self.chunk_size)
                overall_progress = min(99, completed_progress + current_contribution) if self.prompts_executed < self.chunk_size else 100
                
                self.SetProgress(overall_progress)
                self.progress_value = overall_progress
                self.SetStatusMessage(f"Chunk {self.prompts_executed + 1}/{self.chunk_size} - Step {current_step}/{total_steps} ({overall_progress:.2f}%)")
                self.LogInfo(f"Chunk Progress: {overall_progress:.2f}% (Prompt {self.prompts_executed + 1}/{self.chunk_size}, Step {current_step}/{total_steps})")
            else:
                self.SetProgress(percent)
                self.progress_value = percent
                self.SetStatusMessage(f"Step {current_step}/{total_steps} ({percent:.2f}%)")
                self.LogInfo(f"Progress: {percent}% ({current_step}/{total_steps})")
        except ValueError:
            self.LogWarning(f"Could not parse progress from: {self.GetRegexMatch(0)}")
    
    def HandleStdoutProgressPercent(self):
        """Handle progress in the format 'Progress: 45.5%'"""
        try:
            percent = float(self.GetRegexMatch(1))
            
            # Calculate overall chunk progress if needed
            if self.chunk_size > 1:
                completed_progress = (self.prompts_executed / self.chunk_size) * 100
                current_contribution = (percent / self.chunk_size)
                overall_progress = min(99, completed_progress + current_contribution) if self.prompts_executed < self.chunk_size else 100
                
                self.SetProgress(overall_progress)
                self.progress_value = overall_progress
                self.SetStatusMessage(f"Chunk {self.prompts_executed + 1}/{self.chunk_size} - Rendering: {overall_progress:.2f}%")
                self.LogInfo(f"Chunk Progress: {overall_progress:.2f}% (Prompt {self.prompts_executed + 1}/{self.chunk_size} at {percent:.1f}%)")
            else:
                self.SetProgress(percent)
                self.progress_value = percent
                self.SetStatusMessage(f"Rendering: {percent:.2f}%")
                self.LogInfo(f"Progress: {percent}%")
        except ValueError:
            self.LogWarning(f"Could not parse progress from: {self.GetRegexMatch(0)}")
    
    def HandleStdoutPromptExecuted(self):
        """Handle completion message 'Prompt executed in X seconds'"""
        execution_time = self.GetRegexMatch(1)
        self.LogInfo(f"Workflow completed in {execution_time} seconds")
        
        # Check if we already counted this prompt
        if self.prompt_id and self.prompt_id in self.completed_prompts:
            self.LogInfo(f"Prompt {self.prompt_id} already counted")
        else:
            self._handle_stdout_prompt_completion()

    def _handle_stdout_prompt_completion(self):
        """Handle prompt completion detected from stdout"""
        if self.prompt_id:
            self.completed_prompts.add(self.prompt_id)
        
        self.prompts_executed += 1
        self.LogInfo(f"Prompt execution {self.prompts_executed} of {self.chunk_size} completed")
        
        # Move to next prompt
        if self.prompt_id:
            self._move_to_next_prompt()
        
        # Check if all prompts completed
        if self.prompts_executed >= self.chunk_size:
            self._complete_task()
            time.sleep(FILE_WRITE_DELAY)  # Wait for files to be written
        else:
            self._update_progress()
            self.LogInfo(f"Waiting for remaining prompts. {self.prompts_executed} of {self.chunk_size} completed")

    def _is_non_critical_error(self, error_msg: str) -> bool:
        """
        Check if an error message is non-critical and shouldn't fail the task.

        Args:
            error_msg: The error message from ComfyUI stdout

        Returns:
            bool: True if the error is non-critical and should be ignored
        """
        non_critical_patterns = [
            # Missing triton module during xformers initialization
            "ModuleNotFoundError: No module named 'triton'",
            # ImportError variant of missing triton
            "ImportError: No module named 'triton'",
            # A matching Triton is not available warning
            "A matching Triton is not available, some optimizations will not be enabled",
            # xformers version warnings
            "WARNING: You need pytorch with cu130 or higher to use optimized CUDA operations",
            # ComfyUI-Manager imports GitPython on startup, but render workers do not need git.exe.
            "ImportError: Bad git executable",
            "ImportError: Failed to initialize: Bad git executable",
            "Cannot import C:\\AI\\ComfyUI_windows_portable4\\ComfyUI\\custom_nodes\\ComfyUI-Manager module for custom nodes: Failed to initialize: Bad git executable",
        ]

        for pattern in non_critical_patterns:
            if pattern.lower() in error_msg.lower():
                return True

        return False

    def HandleStdoutError(self):
        """Handle errors from ComfyUI"""
        error_msg = self.GetRegexMatch(0)

        # Filter out non-critical errors that shouldn't fail the task
        if self._is_non_critical_error(error_msg):
            self.LogInfo(f"Non-critical ComfyUI warning (continuing): {error_msg}")
            return

        self.LogWarning(f"ComfyUI error: {error_msg}")

        # Optional custom-node imports can log caught Python exceptions while
        # ComfyUI starts successfully. Missing required nodes are checked when
        # the API validates the prompt; a fatal startup still exits the process.
        if not self.server_started:
            return

        if not self.task_completed:
            self.FailRender(f"ComfyUI error: {error_msg}")

    def initialize_api_connection(self) -> bool:
        """Initialize connection to ComfyUI API and get client ID"""
        try:
            time.sleep(2)  # Wait for server to be fully initialized
            
            response = self.http_request(f"{self.comfyui_api_url}/prompt")
            if response['status_code'] != 200:
                self.LogWarning(f"Error connecting to ComfyUI API: {response['status_code']}")
                raise ComfyUIError(f"Error connecting to ComfyUI API: {response['status_code']}")
                return False
                
            self.client_id = f"deadline-{uuid.uuid4().hex}"
            self.LogInfo(f"Got client ID: {self.client_id}")
            object_info_response = self.http_request(f"{self.comfyui_api_url}/object_info")
            if object_info_response['status_code'] != 200:
                raise ComfyUIError(
                    f"Worker {self.GetSlaveName()} did not return /object_info "
                    f"(HTTP {object_info_response['status_code']})."
                )
            self.object_info = object_info_response['json']()
            if not isinstance(self.object_info, dict) or not self.object_info:
                raise ComfyUIError(f"Worker {self.GetSlaveName()} returned invalid /object_info data.")
            return True
        except Exception as e:
            self.LogWarning(f"Error initializing API connection: {e}")
            raise ComfyUIError(f"Error initializing API connection: {str(e)}")
            return False
    
    def queue_workflow(self, workflow_data: dict) -> bool:
        """Submit workflow to ComfyUI queue"""
        try:
            self._reset_prompt_tracking()

            for variation_index in self.assigned_variation_indices:
                prompt_workflow, metadata, workflow_metadata = self._prepare_variation_prompt(workflow_data, variation_index)
                prompt_workflow, expected_outputs = self._preflight_worker_payload(
                    prompt_workflow, workflow_metadata
                )
                if not self._queue_prompt(prompt_workflow, metadata, workflow_metadata):
                    return False
                self.expected_outputs_by_prompt[self.prompt_id] = expected_outputs
                time.sleep(0.2)

            self.LogInfo(f"Queued total of {len(self.prompt_ids)} prompts: {self.prompt_ids}")
            return True
        except Exception as e:
            self.LogWarning(f"Error queuing workflow: {e}")
            raise ComfyUIError(f"Error queuing workflow: {str(e)}")
            return False

    def _reset_prompt_tracking(self):
        """Reset prompt tracking variables"""
        self.prompt_ids = []
        self.completed_prompts = set()
        self.current_tracking_index = 0
        self.expected_outputs_by_prompt = {}

    def _workflow_title_map(self, workflow_metadata=None):
        """Return API node id -> human title from the full UI workflow."""
        workflow = workflow_metadata if workflow_metadata is not None else self.standard_workflow
        result = {}
        if not isinstance(workflow, dict):
            return result
        for node in workflow.get("nodes", []):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", ""))
            properties = node.get("properties", {}) if isinstance(node.get("properties"), dict) else {}
            result[node_id] = str(node.get("title") or properties.get("title") or node.get("type") or "")
        return result

    def _node_context(self, node_id, prompt=None, workflow_metadata=None, class_type=None):
        node_id = str(node_id) if node_id is not None else "unknown"
        prompt = prompt or getattr(self, "current_prompt_payload", {}) or {}
        node = prompt.get(node_id, {}) if isinstance(prompt, dict) else {}
        class_type = class_type or (node.get("class_type") if isinstance(node, dict) else None) or "unknown"
        title = self._workflow_title_map(workflow_metadata).get(node_id) or class_type
        return f"node {node_id} ('{title}', class_type={class_type}) on worker {self.GetSlaveName()}"

    @staticmethod
    def _is_link(value):
        return isinstance(value, (list, tuple)) and len(value) == 2 and str(value[0]) != ""

    def _resolve_fixed_switches(self, prompt, workflow_metadata=None):
        """Remove supported constant-selection switches and reconnect consumers."""
        for switch_id, switch in list(prompt.items()):
            if not isinstance(switch, dict):
                continue
            class_type = switch.get("class_type")
            spec = FIXED_SWITCH_SPECS.get(class_type)
            if not spec:
                continue
            inputs = switch.get("inputs", {})
            raw_select = inputs.get(spec["select"])
            if self._is_link(raw_select):
                raise ComfyUIError(
                    f"Headless preflight rejected dynamic switch {self._node_context(switch_id, prompt, workflow_metadata)}: "
                    "the selection is connected and cannot be proven fixed."
                )
            try:
                selected_index = int(raw_select)
            except (TypeError, ValueError):
                raise ComfyUIError(
                    f"Headless preflight rejected {self._node_context(switch_id, prompt, workflow_metadata)}: "
                    f"invalid fixed selection {raw_select!r}."
                )
            input_name = spec["input"].format(index=selected_index)
            selected_value = inputs.get(input_name)
            if not self._is_link(selected_value):
                raise ComfyUIError(
                    f"Headless preflight rejected {self._node_context(switch_id, prompt, workflow_metadata)}: "
                    f"selected input '{input_name}' is not connected."
                )

            replacements = {0: list(selected_value)}
            for output_index, value_spec in spec.get("constants", {}).items():
                replacements[output_index] = (
                    input_name if value_spec == "input{index}" else selected_index
                )
            consumers = 0
            for node in prompt.values():
                if not isinstance(node, dict):
                    continue
                for key, value in list(node.get("inputs", {}).items()):
                    if self._is_link(value) and str(value[0]) == str(switch_id):
                        output_index = int(value[1])
                        if output_index not in replacements:
                            raise ComfyUIError(
                                f"Headless preflight cannot remove {self._node_context(switch_id, prompt, workflow_metadata)}: "
                                f"consumer uses unsupported output {output_index}."
                            )
                        node["inputs"][key] = copy.deepcopy(replacements[output_index])
                        consumers += 1
            del prompt[switch_id]
            self.LogInfo(
                f"Headless preflight resolved fixed {class_type} node {switch_id} "
                f"to {input_name} and rewired {consumers} consumer(s)."
            )
        return prompt

    def _preflight_worker_payload(self, prompt, workflow_metadata=None):
        """Validate the exact, post-staging and post-variation API payload."""
        if not isinstance(prompt, dict) or not prompt:
            raise ComfyUIError(f"Headless preflight received an empty prompt on worker {self.GetSlaveName()}.")
        prompt = self._resolve_fixed_switches(prompt, workflow_metadata)
        object_info = self.object_info or {}
        expected_outputs = []
        for node_id, node in prompt.items():
            if not isinstance(node, dict):
                raise ComfyUIError(f"Headless preflight found malformed node {node_id} on worker {self.GetSlaveName()}.")
            class_type = node.get("class_type")
            context = self._node_context(node_id, prompt, workflow_metadata, class_type)
            if not class_type or class_type not in object_info:
                raise ComfyUIError(f"Headless preflight failed: {context} is not installed in /object_info.")
            if class_type in KNOWN_UI_DEPENDENT_NODE_TYPES:
                raise ComfyUIError(
                    f"Headless preflight rejected {context}: this node requires an interactive browser/frontend decision."
                )
            definition = object_info[class_type]
            if class_type in WORKFLOW_METADATA_REQUIRED_NODE_TYPES and not isinstance(workflow_metadata, dict):
                raise ComfyUIError(
                    f"Headless preflight rejected {context}: this node reads GUI graph state but full "
                    "extra_pnginfo.workflow metadata was not included in the submission."
                )
            declared_inputs = definition.get("input", {}) if isinstance(definition, dict) else {}
            for section in ("required", "optional"):
                for input_name, input_spec in declared_inputs.get(section, {}).items():
                    options = input_spec[1] if isinstance(input_spec, list) and len(input_spec) > 1 and isinstance(input_spec[1], dict) else {}
                    is_staged_file = bool(options.get("image_upload")) or (
                        class_type in {"LoadImage", "LoadImageMask", "LoadAudio", "LoadVideo"}
                        and input_name in {"image", "audio", "video", "file"}
                    )
                    value = node.get("inputs", {}).get(input_name)
                    if not is_staged_file or not isinstance(value, str):
                        continue
                    clean_value = re.sub(r"\s+\[(input|output|temp)\]\s*$", "", value.strip())
                    root = self.reuse_gui_input_root if self.use_existing_comfyui else self.comfyui_input_dir
                    candidate = os.path.abspath(os.path.join(root, clean_value))
                    try:
                        safe = not os.path.isabs(clean_value) and os.path.commonpath([os.path.abspath(root), candidate]) == os.path.abspath(root)
                    except ValueError:
                        safe = False
                    if not safe or not os.path.isfile(candidate):
                        raise ComfyUIError(
                            f"Headless preflight rejected invalid staged path on {context}, input '{input_name}': {value}"
                        )
            if definition.get("output_node") and re.search(r"save|preview|video|image|audio", class_type, re.I):
                expected_outputs.append(str(node_id))

        worker_mode, distributed_mode, _ = get_distributed_config_for_plugin(self)
        is_registration_prompt = (
            worker_mode
            and distributed_mode
            and len(prompt) == 1
            and next(iter(prompt.values())).get("class_type") == "DeadlineWorkerRegistration"
        )
        if not expected_outputs and not is_registration_prompt:
            raise ComfyUIError(
                f"Headless preflight found no file-producing output node on worker {self.GetSlaveName()}."
            )
        self.current_prompt_payload = prompt
        if is_registration_prompt:
            self.LogInfo(
                f"Headless preflight accepted the distributed worker registration prompt on "
                f"{self.GetSlaveName()}; registration completes through ComfyUI history and does not write a file."
            )
            return prompt, expected_outputs
        self.LogInfo(
            f"Headless preflight passed for {len(prompt)} node(s) on worker {self.GetSlaveName()}; "
            f"expected output nodes: {', '.join(expected_outputs)}"
        )
        return prompt, expected_outputs

    def _queue_prompt(self, workflow_data: dict, deadline_metadata: dict, workflow_metadata: dict = None) -> bool:
        """Queue one prepared variation prompt to ComfyUI."""
        extra_pnginfo = {"deadline": deadline_metadata}
        if workflow_metadata is not None:
            extra_pnginfo["workflow"] = workflow_metadata

        data = {
            "prompt": workflow_data,
            "client_id": self.client_id,
            "extra_data": {
                "extra_pnginfo": extra_pnginfo
            },
        }
        response = self.http_request(f"{self.comfyui_api_url}/prompt", method="POST", data=data)
        
        if response['status_code'] != 200:
            self.LogWarning(f"Error queuing prompt: {response['text']}")
            try:
                response_data = response['json']()
            except Exception:
                response_data = response.get('text', '')
            raise ComfyUIError(
                "ComfyUI prompt validation failed: "
                + self._format_comfy_error(response_data, workflow_data, workflow_metadata)
            )
            return False
        
        self.prompt_id = response['json']()['prompt_id']
        self.prompt_ids.append(self.prompt_id)
        self.LogInfo(f"Queued variation {deadline_metadata['variation_index']} with prompt ID: {self.prompt_id}")
        self.workflow_submitted = True
        return True

    def _prepare_variation_prompt(self, workflow_data: dict, variation_index: int):
        """Deep-copy and rewrite Deadline seed nodes for one global variation index."""
        prompt_workflow = copy.deepcopy(workflow_data)
        if self.use_existing_comfyui:
            self._stage_reused_endpoint_inputs(prompt_workflow)
        seeds = []

        for node_id, node in prompt_workflow.items():
            if not isinstance(node, dict) or node.get("class_type") not in DEADLINE_SEED_NODE_TYPES:
                continue
            node_type = node.get("class_type")
            inputs = node.setdefault("inputs", {})
            base_seed = int(inputs.get("seed", 0))
            actual_seed = base_seed + int(variation_index)
            inputs["seed"] = actual_seed
            inputs["task_id"] = 0
            inputs["batch_mode"] = False
            seeds.append({
                "node_id": str(node_id),
                "base_seed": base_seed,
                "actual_seed": actual_seed,
            })
            self.LogInfo(f"{node_type} node {node_id}: base {base_seed}, variation {variation_index}, actual {actual_seed}")

        metadata = {
            "job_id": getattr(self.GetJob(), "JobId", ""),
            "task_id": str(self.GetCurrentTaskId()),
            "variation_index": int(variation_index),
            "submission_id": self.submission_id,
            "output_directory": self.comfyui_output_dir,
            "input_directory": self.comfyui_input_dir,
            "input_manifest": self.input_manifest_file,
            "seeds": seeds,
        }
        if seeds:
            metadata["base_seed"] = seeds[0]["base_seed"]
            metadata["actual_seed"] = seeds[0]["actual_seed"]

        workflow_metadata = self._prepare_standard_workflow_metadata(seeds)
        return prompt_workflow, metadata, workflow_metadata

    def _stage_reused_endpoint_inputs(self, workflow):
        """Stage only proven task inputs beneath the existing GUI input root."""
        if not self.comfyui_input_dir:
            return
        root = os.path.normcase(os.path.abspath(self.comfyui_input_dir))
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs", {})
            for key, value in list(inputs.items()):
                if not isinstance(value, str) or os.path.isabs(value):
                    continue
                candidate = os.path.normcase(os.path.abspath(os.path.join(root, value)))
                if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
                    task_root = os.path.join(self.reuse_gui_input_root, "deadline", self.submission_id or uuid.uuid4().hex)
                    relative = os.path.relpath(candidate, root)
                    destination = os.path.normcase(os.path.abspath(os.path.join(task_root, relative)))
                    gui_root = os.path.normcase(os.path.abspath(self.reuse_gui_input_root))
                    if not destination.startswith(gui_root + os.sep):
                        raise ComfyUIError("Unsafe GUI input staging path.")
                    try:
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        shutil.copy2(candidate, destination)
                    except OSError as e:
                        raise ComfyUIError(f"Could not stage reused endpoint input '{value}': {e}")
                    inputs[key] = os.path.relpath(destination, gui_root).replace("\\", "/")
                    self.LogInfo(f"Reused endpoint input staged under GUI input root: {inputs[key]}")

    def _prepare_standard_workflow_metadata(self, seeds: list):
        """Patch the UI workflow metadata so dropped output images reopen the actual variation."""
        if self.standard_workflow is None:
            return None

        workflow_metadata = copy.deepcopy(self.standard_workflow)
        seed_by_node_id = {
            str(seed_info["node_id"]): seed_info["actual_seed"]
            for seed_info in seeds
        }

        nodes = workflow_metadata.get("nodes", [])
        if isinstance(nodes, list):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_id = str(node.get("id", ""))
                node_type = node.get("type") or node.get("class_type")
                if node_type not in DEADLINE_SEED_NODE_TYPES or node_id not in seed_by_node_id:
                    continue
                widgets = node.get("widgets_values")
                if isinstance(widgets, list) and widgets:
                    widgets[0] = seed_by_node_id[node_id]

        return workflow_metadata

    def process_history_data(self, history_data: dict) -> bool:
        """Process history data and update task status"""
        if self.prompt_id not in history_data:
            return False
            
        entry = history_data[self.prompt_id]
        status = entry.get('status', {})
        self._handle_prompt_status(status)
        # Failed history can contain partial outputs. Only explicit success counts.
        if status.get('status_str') == 'success' and status.get('completed') is True:
            return self._handle_prompt_completion(entry.get('outputs', {}))
        return False

    def _format_comfy_error(self, error, prompt=None, workflow_metadata=None):
        """Keep ComfyUI's original error and prefix any available node context."""
        node_id = None
        class_type = None
        if isinstance(error, list):
            messages = error
        elif isinstance(error, dict):
            node_id = error.get("node_id")
            class_type = error.get("node_type") or error.get("class_type")
            node_errors = error.get("node_errors")
            if isinstance(node_errors, dict) and node_errors:
                node_id, details = next(iter(node_errors.items()))
                if isinstance(details, dict):
                    class_type = details.get("class_type") or class_type
            messages = error.get("messages")
        else:
            messages = None
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, (list, tuple)) or len(message) < 2 or not isinstance(message[1], dict):
                    continue
                details = message[1]
                node_id = details.get("node_id", node_id)
                class_type = details.get("node_type", class_type)
                if node_id is not None:
                    break
        original = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False, default=str)
        if node_id is None:
            return f"worker {self.GetSlaveName()}: {original}"
        return f"{self._node_context(node_id, prompt, workflow_metadata, class_type)}: {original}"

    def _handle_prompt_completion(self, outputs: dict) -> bool:
        """Handle completed prompt outputs"""
        self.LogInfo(f"Workflow complete: Found outputs in history for prompt {self.prompt_id}")
        
        self._validate_expected_outputs(outputs)

        # Mark prompt as completed only after the expected artifacts exist.
        if self.prompt_id not in self.completed_prompts:
            self.completed_prompts.add(self.prompt_id)
            self.prompts_executed += 1
            self.LogInfo(f"Prompt {self.prompt_id} execution {self.prompts_executed} of {self.chunk_size} completed")
        
        # Log output information
        self._log_output_information(outputs)
        
        # Check if all prompts completed
        if self.prompts_executed >= self.chunk_size:
            self._complete_task()
            return True
        else:
            self._move_to_next_prompt()
            self._update_progress()
            return False

    def _log_output_information(self, outputs: dict):
        """Log information about generated outputs"""
        output_nodes = []
        for node_id, node_outputs in outputs.items():
            if 'images' in node_outputs:
                output_nodes.append(node_id)
                for img in node_outputs['images']:
                    self.LogInfo(f"Generated image: {img['filename']}")
        
        if output_nodes:
            self.LogInfo(f"Output producing nodes: {output_nodes}")

        if self.use_existing_comfyui and self.custom_output_dir_specified:
            gui_output = self.reuse_gui_output_root
            output_root = os.path.normcase(os.path.abspath(gui_output))
            target_root = os.path.normcase(os.path.abspath(self.comfyui_output_dir))
            copied = 0
            for node_outputs in outputs.values():
                for group in ("images", "gifs", "videos"):
                    for item in node_outputs.get(group, []):
                        if item.get("type", "output") != "output":
                            continue
                        filename = item.get("filename")
                        if not filename:
                            continue
                        relative = os.path.join(item.get("subfolder", ""), filename)
                        source = os.path.normcase(os.path.abspath(os.path.join(gui_output, relative)))
                        target = os.path.normcase(os.path.abspath(os.path.join(self.comfyui_output_dir, relative)))
                        if not source.startswith(output_root + os.sep) or not target.startswith(target_root + os.sep):
                            raise ComfyUIError(f"ComfyUI history returned unsafe output path: {relative}")
                        if not os.path.isfile(source):
                            raise ComfyUIError(f"Expected this task's output is missing from GUI endpoint: {relative}")
                        try:
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            shutil.copy2(source, target)
                        except OSError as e:
                            raise ComfyUIError(f"Could not copy this task's GUI output '{relative}': {e}")
                        copied += 1
                        self.LogInfo(f"Copied this task's GUI output to job directory: {relative}")
            if copied == 0:
                raise ComfyUIError("Reused endpoint completed without an output file recorded for this task.")

    def _validate_expected_outputs(self, outputs):
        expected = getattr(self, "expected_outputs_by_prompt", {}).get(self.prompt_id, [])
        if not expected:
            return
        missing_nodes = [node_id for node_id in expected if not isinstance(outputs.get(node_id), dict)]
        file_items = []
        for node_id in expected:
            node_outputs = outputs.get(node_id, {})
            for group in FILE_OUTPUT_GROUPS:
                for item in node_outputs.get(group, []):
                    if isinstance(item, dict) and item.get("filename"):
                        file_items.append((node_id, item))
        if missing_nodes or not file_items:
            context = ", ".join(self._node_context(node_id) for node_id in (missing_nodes or expected))
            raise ComfyUIError(
                f"ComfyUI reported success but expected output is missing for {context}. "
                f"History outputs: {json.dumps(outputs, ensure_ascii=False, default=str)}"
            )

        output_root = self.reuse_gui_output_root if self.use_existing_comfyui else self.comfyui_output_dir
        temp_root = os.path.join(self.comfyui_install_path, "ComfyUI", "temp")
        for node_id, item in file_items:
            kind = item.get("type", "output")
            root = temp_root if kind == "temp" else output_root
            relative = os.path.join(str(item.get("subfolder", "")), str(item["filename"]))
            candidate = os.path.abspath(os.path.join(root, relative))
            normalized_root = os.path.abspath(root)
            try:
                safe = os.path.commonpath([normalized_root, candidate]) == normalized_root
            except ValueError:
                safe = False
            if not safe or not os.path.isfile(candidate):
                raise ComfyUIError(
                    f"ComfyUI reported success but expected output file is missing for "
                    f"{self._node_context(node_id)}: {candidate}"
                )

    def _complete_task(self):
        """Mark task as complete"""
        self.SetProgress(100)
        self.SetStatusMessage("Finished Render")
        self.task_completed = True
        self.LogInfo(f"All {self.chunk_size} prompt(s) in this task completed")
        if self.use_existing_comfyui:
            self._signal_reuse_waiter(True)

    def _move_to_next_prompt(self):
        """Move to tracking the next prompt"""
        self.current_tracking_index += 1
        if self.current_tracking_index < len(self.prompt_ids):
            self.prompt_id = self.prompt_ids[self.current_tracking_index]
            self.LogInfo(f"Moving to track next prompt: {self.prompt_id}")
        else:
            self.prompt_id = None
            self.LogInfo(f"No more prompts to track. Waiting for {self.chunk_size - self.prompts_executed} more executions.")

    def _update_progress(self):
        """Update progress based on completed prompts"""
        progress_percent = (self.prompts_executed / self.chunk_size) * 100
        self.SetProgress(progress_percent)
        self.SetStatusMessage(f"Completed {self.prompts_executed} of {self.chunk_size} prompts ({progress_percent:.1f}%)")

    def _handle_prompt_status(self, status: dict) -> bool:
        """Handle prompt status information"""
        if status.get('status_str', status.get('status')) == 'error' or any(
                msg[0] in ('execution_error', 'execution_interrupted')
                for msg in status.get('messages', []) if isinstance(msg, (list, tuple)) and msg):
            return self._handle_prompt_error(status)
        
        # Update progress from execution status
        if 'exec_info' in status and 'progress' in status['exec_info']:
            self._update_execution_progress(status['exec_info']['progress'])
            
        return False

    def _handle_prompt_error(self, status: dict) -> bool:
        """Handle prompt execution errors"""
        error_data = status.get('error') or status.get('messages', status)
        error_msg = self._format_comfy_error(error_data)
        self.LogWarning(f"ComfyUI reported error for prompt {self.prompt_id}: {error_msg}")
        raise ComfyUIError(f"ComfyUI workflow failed: {error_msg}")
        return True

    def _update_execution_progress(self, progress: float):
        """Update progress from execution information"""
        current_prompt_progress = float(progress) * 100
        
        # Calculate overall chunk progress if needed
        if self.chunk_size > 1:
            completed_progress = (self.prompts_executed / self.chunk_size) * 100
            current_contribution = (current_prompt_progress / self.chunk_size)
            overall_progress = min(99, completed_progress + current_contribution) if self.prompts_executed < self.chunk_size else 100
            
            self.SetProgress(overall_progress)
            self.progress_value = overall_progress
        else:
            self.SetProgress(current_prompt_progress)
            self.progress_value = current_prompt_progress

    def signal_task_completion(self):
        """Signal to Deadline that the task is complete"""
        try:
            job = self.GetJob()
            task_id = self.GetCurrentTaskId()
            slave_name = self.GetSlaveName()
            self.LogInfo(f"Signaling Deadline that task {task_id} for job {job.JobId} is complete")
            
            tasks = RepositoryUtils.GetJobTasks(job, True)
            current_task = self._find_current_task(tasks, task_id)
            
            if current_task:
                self.LogInfo(f"Completing task {current_task.TaskID}")
                RepositoryUtils.CompleteTasks(job, [current_task], slave_name)
            else:
                self.LogWarning(f"Could not find task with ID {task_id}")
        except Exception as e:
            self.LogWarning(f"Error signaling task completion: {e}")
            self.LogWarning(traceback.format_exc())

    def signal_task_failure(self, message):
        """Fail the active repository task from the submission thread."""
        try:
            job = self.GetJob()
            task_id = self.GetCurrentTaskId()
            slave_name = self.GetSlaveName()
            tasks = RepositoryUtils.GetJobTasks(job, True)
            current_task = self._find_current_task(tasks, task_id)
            if current_task:
                self.LogWarning(
                    f"Failing Deadline task {current_task.TaskID} on {slave_name}: {message}"
                )
                RepositoryUtils.FailTasks(job, [current_task], slave_name)
            else:
                self.LogWarning(f"Could not find task {task_id} to fail: {message}")
        except Exception as e:
            self.LogWarning(f"Error signaling task failure: {e}")
            self.LogWarning(traceback.format_exc())

    def _find_current_task(self, tasks, task_id):
        """Find the current task in the task list"""
        for task in tasks:
            if str(task.TaskID) == str(task_id):
                return task
        return None

    def monitor_workflow_execution(self) -> bool:
        """Poll history endpoint and wait for workflow completion"""
        self.LogInfo(f"Beginning to monitor workflow execution for chunk size {self.chunk_size}")
        self.LogInfo(f"Monitoring prompts in this order: {self.prompt_ids}")
        
        if self.prompt_ids:
            self.prompt_id = self.prompt_ids[0]
        
        poll_count = 0
        
        while self.thread_running:
            if self.task_completed:
                self.LogInfo("Task already marked as complete")
                
                # Check if we're in distributed worker mode
                worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
                
                if worker_mode and distributed_mode:
                    self.LogInfo("Distributed worker mode: Registration completed, entering keep-alive mode")
                    self._enter_distributed_keep_alive_mode()
                else:
                    if not self.use_existing_comfyui:
                        self.signal_task_completion()
                return True
            
            if self.prompt_id:
                if self._poll_prompt_status(poll_count):
                    break
            else:
                self._check_for_missed_prompts()
            
            poll_count += 1
            time.sleep(DEFAULT_POLLING_INTERVAL)
        
        if self.task_completed:
            # Check if we're in distributed worker mode
            worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
            
            if worker_mode and distributed_mode:
                self.LogInfo("Distributed worker mode: Registration workflow completed, entering keep-alive mode")
                self.LogInfo("Task will remain active to process distributed workflows from master")
                
                # Don't signal completion - enter keep-alive mode instead
                self._enter_distributed_keep_alive_mode()
            else:
                # Normal mode - complete the task
                if not self.use_existing_comfyui:
                    self.signal_task_completion()
        
        return self.task_completed

    def _enter_distributed_keep_alive_mode(self):
        """Enter keep-alive mode for distributed workers"""
        import time
        import threading
        
        self.LogInfo("🔄 Entering distributed worker keep-alive mode...")
        self.LogInfo("Worker will remain active until manually stopped or job is cancelled")
        
        def keep_alive_loop():
            """Keep the task alive indefinitely"""
            try:
                while True:
                    self.LogInfo("🔄 Distributed worker is alive and ready for workflows...")
                    time.sleep(300)  # Log every 5 minutes
            except KeyboardInterrupt:
                self.LogInfo("🛑 Keep-alive interrupted by user")
            except Exception as e:
                self.LogInfo(f"❌ Keep-alive error: {e}")
        
        # Start keep-alive in daemon thread  
        keep_alive_thread = threading.Thread(target=keep_alive_loop, daemon=True)
        keep_alive_thread.start()
        
        try:
            # Block main thread indefinitely
            self.LogInfo("🔄 Main thread entering infinite wait...")
            while True:
                time.sleep(60)  # Check every minute
        except KeyboardInterrupt:
            self.LogInfo("🛑 Distributed worker keep-alive interrupted")
        except Exception as e:
            self.LogInfo(f"❌ Distributed worker keep-alive error: {e}")

    def _poll_prompt_status(self, poll_count: int) -> bool:
        """Poll the status of the current prompt"""
        try:
            verbose_log = (poll_count == 0) or (poll_count % PROGRESS_LOG_INTERVAL == 0)
            history_response = self.http_request(f"{self.comfyui_api_url}/history/{self.prompt_id}", verbose=verbose_log)
            
            if history_response['status_code'] == 200:
                history_data = history_response['json']()
                if self.process_history_data(history_data):
                    return True  # Task completed
                    
                if verbose_log and self.progress_value > 0:
                    self.SetStatusMessage(f"Executing: {self.progress_value:.1f}%")
                    
            elif history_response['status_code'] == 404:
                if verbose_log:
                    self.LogInfo(f"History entry not found yet for prompt {self.prompt_id}")
            else:
                if verbose_log:
                    self.LogWarning(f"Unexpected response from history endpoint: {history_response['status_code']}")
        except ComfyUIError:
            raise
        except Exception as e:
            self.LogWarning(f"Error checking history endpoint: {e}")
        
        return False

    def _check_for_missed_prompts(self):
        """Check for any completed prompts that weren't tracked"""
        if self.prompts_executed >= self.chunk_size:
            return
            
        try:
            history_response = self.http_request(f"{self.comfyui_api_url}/history", verbose=False)
            if history_response['status_code'] == 200:
                all_history = history_response['json']()
                
                for i, prompt_id in enumerate(self.prompt_ids):
                    if prompt_id in self.completed_prompts:
                        continue
                    
                    if prompt_id in all_history and 'outputs' in all_history[prompt_id]:
                        self.LogInfo(f"Found completed prompt {prompt_id} that wasn't tracked")
                        self.prompt_id = prompt_id
                        self.current_tracking_index = i
                        break
        except Exception as e:
            self.LogWarning(f"Error looking for completed prompts: {e}")

    def submit_workflow(self):
        """Submit the workflow to ComfyUI's API and wait for completion"""
        try:
            workflow_data = self.load_and_validate_workflow()
            if not workflow_data:
                raise ComfyUIError("Workflow could not be loaded or validated.")

            self._load_standard_workflow_metadata()
            
            if not self.initialize_api_connection():
                raise ComfyUIError("Could not initialize the ComfyUI API connection.")
            
            if not self.queue_workflow(workflow_data):
                raise ComfyUIError("ComfyUI did not accept this task's prompt.")
            
            self.monitor_workflow_execution()
            
        except Exception as e:
            self.submission_error = f"Error during workflow submission: {str(e)}"
            self.LogWarning(self.submission_error)
            traceback.print_exc()
            self.thread_running = False
            self.task_completed = False
            self._signal_reuse_waiter(False)
            # Deadline render callbacks run on this background thread. Explicitly
            # fail the repository task so an exception here cannot leave it active.
            self.signal_task_failure(self.submission_error)
            self.AbortRender(self.submission_error)
