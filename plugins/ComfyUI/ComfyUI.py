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
DEFAULT_TIMEOUT = 6000  # 100 minutes
DEFAULT_POLLING_INTERVAL = 10  # seconds
MAX_SEED_VALUE = 2147483647
PROGRESS_LOG_INTERVAL = 10  # Log every 10 polls
FILE_WRITE_DELAY = 2  # seconds to wait for files to be written

# Seed parameter names to search for in workflows
SEED_PARAMETER_NAMES = ["seed", "noise_seed", "value"]

# Output node types that indicate the workflow will produce output
OUTPUT_NODE_TYPES = ["SaveImage", "PreviewImage", "SaveVideo"]

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
        self.AddStdoutHandlerCallback(r"Prompt executed in ([0-9.]+) seconds").HandleCallback += self.HandleStdoutPromptExecuted

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
        
        # Batch processing variables
        self.chunk_size = 1
        self.prompts_executed = 0
        self.batch_mode = False
        
        # Prompt tracking variables
        self.prompt_ids = []
        self.completed_prompts = set()
        self.current_tracking_index = 0

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
        self.SingleFramesOnly = True
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

        # 2. Check Deadline worker GPU affinity if not set by plugin info
        if assigned_gpu is None:
            assigned_gpu = self._get_gpu_from_worker_affinity()

        # 3. Use default device if still no assignment
        if assigned_gpu is None:
            assigned_gpu = self._get_default_cuda_device()

        return f"--cuda-device {assigned_gpu}" if assigned_gpu is not None else ""

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
        if self.batch_mode:
            self.chunk_size = int(self.GetJob().ChunkSize)
            self.LogInfo(f"Batch mode enabled. Chunk size: {self.chunk_size}")
        else:
            self.chunk_size = 1
            self.LogInfo("Batch mode disabled. Processing single task.")
        
        self.prompts_executed = 0

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
            comfyui_path = self.GetConfigEntry("ComfyUIPath")
            self.comfyui_output_dir = os.path.join(comfyui_path, "ComfyUI", "output")
            self.custom_output_dir_specified = False
            self.LogInfo(f"Using ComfyUI's default output directory: {self.comfyui_output_dir}")
        
        if not os.path.exists(self.comfyui_output_dir):
            self._create_directory(self.comfyui_output_dir, "default output")

    def _create_directory(self, directory_path: str, description: str):
        """Create a directory with error handling"""
        try:
            os.makedirs(directory_path)
            self.LogInfo(f"Created {description} directory: {directory_path}")
        except Exception as e:
            self.LogWarning(f"Could not create {description} directory {directory_path}: {e}")

    def _calculate_comfyui_port(self) -> str:
        """Calculate the port for ComfyUI based on CUDA device"""
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

    def _determine_final_port(self, base_port: int) -> str:
        """Determine final port to use, checking for existing instances"""
        # Check if we should force a new instance (for distributed workers)
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
        
        if force_new_instance or worker_mode or distributed_mode:
            self.LogInfo("Worker/Distributed mode: Will start new ComfyUI instance (not reusing existing)")
            self.use_existing_comfyui = False
            
            # For workers, use dynamic port allocation to avoid conflicts
            if worker_mode or distributed_mode:
                worker_port = self._calculate_worker_port(base_port)
                self.comfyui_port = self._find_available_port(worker_port)
                self.LogInfo(f"Worker mode: Using port {self.comfyui_port}")
            else:
                self.comfyui_port = self._find_available_port(base_port)
                self.LogInfo(f"Force new instance: Using port {self.comfyui_port}")
                
            self.comfyui_api_url = f"http://127.0.0.1:{self.comfyui_port}"
            return self.comfyui_port
        
        # Normal batch mode logic
        self.LogInfo(f"Checking if ComfyUI is already running on port {base_port}")
        
        if self._is_port_in_use(base_port):
            self.LogInfo(f"ComfyUI is already running on port {base_port}, will use existing instance")
            self.use_existing_comfyui = True
            self.comfyui_port = str(base_port)
            self.comfyui_api_url = f"http://127.0.0.1:{self.comfyui_port}"
            self.server_started = True
        else:
            self.LogInfo(f"No ComfyUI instance detected on port {base_port}")
            self.use_existing_comfyui = False
            self.comfyui_port = self._find_available_port(base_port)
            self.LogInfo(f"Will use port {self.comfyui_port} for ComfyUI")
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
            self._setup_output_directory()
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
        comfyui_path = self.GetConfigEntry("ComfyUIPath")
        python_exe = os.path.join(comfyui_path, "python_embeded", "python.exe")
        
        if os.path.exists(python_exe):
            self.LogInfo(f"Using ComfyUI embedded Python: {python_exe}")
            # Set the Deadline worker name as environment variable for ComfyUI process
            self._set_deadline_environment_variables()
            return python_exe
        else:
            error_msg = f"ComfyUI embedded Python not found at: {python_exe}. Please check your ComfyUIPath configuration."
            self.LogWarning(error_msg)
            self.FailRender(error_msg)
            return ""

    def RenderArgument(self):
        """Build command line arguments for ComfyUI"""
        comfyui_path = self.GetConfigEntry("ComfyUIPath")
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
        self.LogInfo("Using existing ComfyUI instance - returning dummy command.")
        
        # Start workflow submission thread
        workflow_thread = threading.Thread(target=self.submit_workflow)
        workflow_thread.daemon = True
        workflow_thread.start()
        
        dummy_script = self._get_dummy_script()
        return f'-c "{dummy_script}"'

    def _get_dummy_script(self) -> str:
        """Get dummy Python script for workflow waiting"""
        return """
import time, os, sys
print('Waiting for ComfyUI workflow to complete via API...')
max_wait = 600  # 10 minute timeout
for i in range(max_wait):
    if i % 10 == 0:
        progress_val = float(os.environ.get('DEADLINE_TASK_PROGRESS', i * (100.0 / max_wait)))
        print(f'Progress: {progress_val:.1f}%')
    time.sleep(1)
print('Dummy command timeout reached or task completed.')
"""

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

        # Add output directory if custom one was specified
        if self.custom_output_dir_specified and self.comfyui_output_dir:
            args_list.append(f'--output-directory "{self.comfyui_output_dir}"')
            self.LogInfo(f"Passing --output-directory \"{self.comfyui_output_dir}\" to ComfyUI.")
        else:
            self.LogInfo("Not passing --output-directory to ComfyUI, it will use its default.")

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
            with urllib.request.urlopen(req) as response:
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
        Inject task_id and batch_mode into DeadlineDistributedSeed nodes.
        
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
                    
                if node.get("class_type") == "DeadlineSeed":
                    if "inputs" not in node:
                        node["inputs"] = {}
                    
                    # Inject task_id and batch_mode as hidden parameters
                    node["inputs"]["task_id"] = task_id
                    node["inputs"]["batch_mode"] = batch_mode
                    
                    self.LogInfo(f"Injected task_id={task_id}, batch_mode={batch_mode} into DeadlineSeed node {node_id}")
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
            self.FailRender(f"Workflow file not found: {workflow_file}")
            return None
        
        try:
            workflow_data = self._load_workflow_from_file(workflow_file)
            workflow_data = self.validate_workflow(workflow_data)
            
            # Inject parameters for DeadlineDistributedSeed nodes
            deadline_seeds_injected = self.inject_deadline_seed_parameters(workflow_data)
            
            # Apply seed manipulation only if no DeadlineDistributedSeed nodes are present
            if not deadline_seeds_injected:
                task_id = self.GetCurrentTaskId()
                seeds_modified = self.modify_workflow_seeds(workflow_data, task_id)
                
                if seeds_modified:
                    self.LogInfo(f"Applied seed manipulation for task ID {task_id}")
                else:
                    self.LogInfo(f"No seed manipulation applied for task ID {task_id}")
            else:
                self.LogInfo("DeadlineSeed nodes detected - skipping automatic seed modification")
            
            return workflow_data
        except Exception as e:
            self.LogWarning(f"Error loading or validating workflow file '{workflow_file}': {e}")
            self.FailRender(f"Error loading or validating workflow file: {str(e)}")
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
            # Normal batch mode
            workflow_file = self.GetPluginInfoEntryWithDefault("ComfyWorkflowFile", self.GetDataFilename())
        
        workflow_file = RepositoryUtils.CheckPathMapping(workflow_file)
        self.LogInfo(f"Workflow file setting from plugin info: '{workflow_file}'")
        return workflow_file

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

    def HandleStdoutError(self):
        """Handle errors from ComfyUI"""
        error_msg = self.GetRegexMatch(0)
        self.LogWarning(f"ComfyUI error: {error_msg}")
        
        if not self.task_completed:
            self.FailRender(f"ComfyUI error: {error_msg}")

    def initialize_api_connection(self) -> bool:
        """Initialize connection to ComfyUI API and get client ID"""
        try:
            time.sleep(2)  # Wait for server to be fully initialized
            
            response = self.http_request(f"{self.comfyui_api_url}/prompt")
            if response['status_code'] != 200:
                self.LogWarning(f"Error connecting to ComfyUI API: {response['status_code']}")
                self.FailRender(f"Error connecting to ComfyUI API: {response['status_code']}")
                return False
                
            self.client_id = response['json']().get('client_id', '')
            self.LogInfo(f"Got client ID: {self.client_id}")
            return True
        except Exception as e:
            self.LogWarning(f"Error initializing API connection: {e}")
            self.FailRender(f"Error initializing API connection: {str(e)}")
            return False
    
    def queue_workflow(self, workflow_data: dict) -> bool:
        """Submit workflow to ComfyUI queue"""
        try:
            self._reset_prompt_tracking()
            
            # Queue initial prompt
            if not self._queue_single_prompt(workflow_data):
                return False
            
            # Queue additional prompts for batch mode
            if self.batch_mode and self.chunk_size > 1:
                self._queue_batch_prompts(workflow_data)
            
            self.LogInfo(f"Queued total of {len(self.prompt_ids)} prompts: {self.prompt_ids}")
            return True
        except Exception as e:
            self.LogWarning(f"Error queuing workflow: {e}")
            self.FailRender(f"Error queuing workflow: {str(e)}")
            return False

    def _reset_prompt_tracking(self):
        """Reset prompt tracking variables"""
        self.prompt_ids = []
        self.completed_prompts = set()
        self.current_tracking_index = 0

    def _queue_single_prompt(self, workflow_data: dict) -> bool:
        """Queue a single prompt to ComfyUI"""
        data = {"prompt": workflow_data, "client_id": self.client_id}
        response = self.http_request(f"{self.comfyui_api_url}/prompt", method="POST", data=data)
        
        if response['status_code'] != 200:
            self.LogWarning(f"Error queuing prompt: {response['text']}")
            self.FailRender(f"Error queuing prompt: {response['text']}")
            return False
        
        self.prompt_id = response['json']()['prompt_id']
        self.prompt_ids.append(self.prompt_id)
        self.LogInfo(f"Queued prompt with ID: {self.prompt_id}")
        self.workflow_submitted = True
        return True

    def _queue_batch_prompts(self, workflow_data: dict):
        """Queue additional prompts for batch processing"""
        self.LogInfo(f"Batch mode with chunk size {self.chunk_size}. Queueing additional prompts...")
        
        import copy
        
        for i in range(1, self.chunk_size):
            prompt_workflow = copy.deepcopy(workflow_data)
            
            # Check if workflow has DeadlineSeed nodes
            has_deadline_seeds = any(
                node.get("class_type") == "DeadlineSeed" 
                for node in prompt_workflow.values() 
                if isinstance(node, dict)
            )
            
            if has_deadline_seeds:
                # Update task_id for DeadlineSeed nodes (chunk-local indexing)
                for node_id, node in prompt_workflow.items():
                    if isinstance(node, dict) and node.get("class_type") == "DeadlineSeed":
                        if "inputs" not in node:
                            node["inputs"] = {}
                        # Use i as the offset for chunks within the same task
                        base_task_id = int(node["inputs"].get("task_id", 0))
                        node["inputs"]["task_id"] = base_task_id + i
                        self.LogInfo(f"Updated DeadlineSeed node {node_id} task_id to {base_task_id + i}")
            else:
                # Modify seeds using the old method
                if self.GetPluginInfoEntryWithDefault("SeedMode", "fixed") != "fixed":
                    self.modify_workflow_seeds(prompt_workflow, i)
                    self.LogInfo(f"Modified seeds for additional prompt {i}")
            
            # Queue the workflow
            data = {"prompt": prompt_workflow, "client_id": self.client_id}
            response = self.http_request(f"{self.comfyui_api_url}/prompt", method="POST", data=data)
            
            if response['status_code'] != 200:
                self.LogWarning(f"Error queuing additional prompt {i}: {response['text']}")
                break
                
            prompt_id = response['json']()['prompt_id']
            self.prompt_ids.append(prompt_id)
            self.LogInfo(f"Queued additional prompt {i} with ID: {prompt_id}")
            
            time.sleep(0.5)  # Small delay between submissions

    def process_history_data(self, history_data: dict) -> bool:
        """Process history data and update task status"""
        if self.prompt_id not in history_data:
            return False
            
        # Check for outputs indicating completion
        if 'outputs' in history_data[self.prompt_id]:
            return self._handle_prompt_completion(history_data[self.prompt_id]['outputs'])
        
        # Check for error status
        elif 'status' in history_data[self.prompt_id]:
            return self._handle_prompt_status(history_data[self.prompt_id]['status'])
                    
        return False

    def _handle_prompt_completion(self, outputs: dict) -> bool:
        """Handle completed prompt outputs"""
        if not outputs:
            return False
            
        self.LogInfo(f"Workflow complete: Found outputs in history for prompt {self.prompt_id}")
        
        # Mark prompt as completed
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

    def _complete_task(self):
        """Mark task as complete"""
        self.SetProgress(100)
        self.SetStatusMessage("Finished Render")
        self.task_completed = True
        
        # Check if we're in distributed worker mode
        worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
        
        if worker_mode and distributed_mode:
            self.LogInfo("Distributed worker mode: Registration completed, entering keep-alive mode")
            self._enter_distributed_keep_alive_mode()
        else:
            self.signal_task_completion()
            self.LogInfo(f"All {self.chunk_size} prompts in chunk completed, task marked as complete")

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
        if status.get('status') == 'error':
            return self._handle_prompt_error(status)
        
        # Update progress from execution status
        if 'exec_info' in status and 'progress' in status['exec_info']:
            self._update_execution_progress(status['exec_info']['progress'])
            
        return False

    def _handle_prompt_error(self, status: dict) -> bool:
        """Handle prompt execution errors"""
        error_msg = status.get('error', 'Unknown error')
        self.LogWarning(f"ComfyUI reported error for prompt {self.prompt_id}: {error_msg}")
        
        if self.chunk_size > 1:
            # Continue with remaining prompts in batch
            self.LogWarning(f"Continuing with remaining prompts in chunk")
            self.completed_prompts.add(self.prompt_id)
            self._move_to_next_prompt()
            return False
        else:
            # Single prompt mode, fail the task
            self.FailRender(f"ComfyUI workflow failed: {error_msg}")
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

    def _find_current_task(self, tasks, task_id):
        """Find the current task in the task list"""
        for task in tasks:
            if str(task.TaskID) == str(task_id):
                return task
        return None

    def monitor_workflow_execution(self) -> bool:
        """Poll history endpoint and wait for workflow completion"""
        start_time = time.time()
        self.LogInfo(f"Beginning to monitor workflow execution for chunk size {self.chunk_size}")
        self.LogInfo(f"Monitoring prompts in this order: {self.prompt_ids}")
        
        if self.prompt_ids:
            self.prompt_id = self.prompt_ids[0]
        
        poll_count = 0
        
        while time.time() - start_time < DEFAULT_TIMEOUT and self.thread_running:
            if self.task_completed:
                self.LogInfo("Task already marked as complete by stdout handler")
                
                # Check if we're in distributed worker mode
                worker_mode, distributed_mode, force_new_instance = get_distributed_config_for_plugin(self)
                
                if worker_mode and distributed_mode:
                    self.LogInfo("Distributed worker mode: Registration completed, entering keep-alive mode")
                    self._enter_distributed_keep_alive_mode()
                else:
                    self.signal_task_completion()
                return True
            
            if self.prompt_id:
                if self._poll_prompt_status(poll_count):
                    break
            else:
                self._check_for_missed_prompts()
            
            poll_count += 1
            time.sleep(DEFAULT_POLLING_INTERVAL)
        
        # Check for timeout
        if not self.task_completed and time.time() - start_time >= DEFAULT_TIMEOUT:
            self.LogWarning(f"Timeout waiting for workflow to complete after {DEFAULT_TIMEOUT} seconds")
            self.FailRender(f"Timeout waiting for workflow to complete")
            return False
        
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
                return
            
            if not self.initialize_api_connection():
                return
            
            if not self.queue_workflow(workflow_data):
                return
            
            self.monitor_workflow_execution()
            
        except Exception as e:
            self.LogWarning(f"Error during workflow submission: {e}")
            traceback.print_exc()
            self.FailRender(f"Error during workflow submission: {str(e)}") 