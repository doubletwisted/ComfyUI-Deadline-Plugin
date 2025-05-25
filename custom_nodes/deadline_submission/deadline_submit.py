# deadline_submit.py

"""
ComfyUI Deadline Submission Node
by Dominik Bargiel dominikbargiel97@gmail.com

A ComfyUI custom node for submitting workflows to Thinkbox Deadline render farm.
"""

import os
import sys
import json
import tempfile
import subprocess
import uuid
import time
import re
from typing import Optional, Dict, List, Any, Union, Tuple

# Configuration constants
DEADLINE_COMMAND_PATHS = {
    'windows': "C:\\Program Files\\Thinkbox\\Deadline10\\bin\\deadlinecommand.exe",
    'linux': "/opt/Thinkbox/Deadline10/bin/deadlinecommand"
}

# Node configuration constants
class NodeDefaults:
    JOB_NAME = "ComfyUI via DeadlineNode"
    PRIORITY = 50
    POOL = "none"
    GROUP = "none"
    BATCH_COUNT = 1
    CHUNK_SIZE = 1
    MAX_BATCH_COUNT = 100
    MAX_CHUNK_SIZE = 16
    MAX_PRIORITY = 100

class DeadlineCommandHelper:
    """Helper class for interacting with Deadline command line"""
    
    @staticmethod
    def get_deadline_command() -> str:
        """Get the path to the deadlinecommand executable"""
        deadline_bin = ""
        try:
            deadline_bin = os.environ.get('DEADLINE_PATH', '')
        except KeyError:
            pass

        if not deadline_bin and os.path.exists("/Users/Shared/Thinkbox/DEADLINE_PATH"):
            try:
                with open("/Users/Shared/Thinkbox/DEADLINE_PATH") as f:
                    deadline_bin = f.read().strip()
            except Exception:
                pass

        if deadline_bin:
            deadline_command = os.path.join(deadline_bin, "deadlinecommand")
            if os.path.exists(deadline_command):
                return deadline_command

        # Try platform-specific default paths
        if sys.platform.startswith('win'):
            default_path = DEADLINE_COMMAND_PATHS['windows']
        else:
            default_path = DEADLINE_COMMAND_PATHS['linux']
            
        if os.path.exists(default_path):
            return default_path
        
        return ""

    @staticmethod
    def call_deadline_command(arguments: List[str], hide_window: bool = True, read_stdout: bool = True) -> str:
        """Call deadlinecommand with the given arguments"""
        deadline_command = DeadlineCommandHelper.get_deadline_command()
        if not deadline_command:
            raise Exception("Deadline command not found")
            
        startupinfo = None
        creationflags = 0
        
        if os.name == 'nt':
            if hide_window:
                try:
                    startupinfo = subprocess.STARTUPINFO()
                    if hasattr(subprocess, '_subprocess') and hasattr(subprocess._subprocess, 'STARTF_USESHOWWINDOW'):
                        startupinfo.dwFlags |= subprocess._subprocess.STARTF_USESHOWWINDOW
                    elif hasattr(subprocess, 'STARTF_USESHOWWINDOW'):
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                except:
                    pass
            else:
                CREATE_NO_WINDOW = 0x08000000
                creationflags = CREATE_NO_WINDOW
        
        full_arguments = [deadline_command] + arguments
        
        proc = subprocess.Popen(
            full_arguments, 
            stdin=subprocess.PIPE, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            startupinfo=startupinfo, 
            creationflags=creationflags
        )
        
        output = ""
        if read_stdout:
            output, errors = proc.communicate()
            
            if sys.version_info[0] >= 3 and isinstance(output, bytes):
                output = output.decode(errors="replace")
        
        return output

    @staticmethod
    def get_job_id_from_submission(submission_results: str) -> str:
        """Parse the job ID from the submission results"""
        for line in submission_results.split():
            if line.startswith("JobID="):
                return line.replace("JobID=", "").strip()
        return ""

class WorkflowProcessor:
    """Handles workflow data processing and validation"""
    
    @staticmethod
    def normalize_workflow(workflow_data: Union[Dict, List]) -> Optional[Dict]:
        """Normalize workflow data to ensure compatibility"""
        if not workflow_data:
            print("Deadline Submission: Error - Empty workflow data.")
            return None
            
        # If workflow is already in UI format (dictionary with node IDs as keys)
        if isinstance(workflow_data, dict):
            is_ui_format = any(isinstance(key, str) and key.isdigit() for key in workflow_data.keys())
            if is_ui_format:
                return workflow_data
                
        # If it's the API format (list of nodes)
        if isinstance(workflow_data, list):
            return WorkflowProcessor._convert_api_to_ui_format(workflow_data)
            
        # Not recognized format
        print(f"Deadline Submission: Warning - Unrecognized workflow format. Attempting to use as-is.")
        return workflow_data if isinstance(workflow_data, dict) else None

    @staticmethod
    def _convert_api_to_ui_format(workflow_list: List) -> Dict:
        """Convert API format workflow to UI format"""
        ui_format = {}
        for node in workflow_list:
            if isinstance(node, list) and len(node) >= 3:
                node_id = str(node[0])
                ui_format[node_id] = {
                    "class_type": node[1],
                    "inputs": node[2]
                }
        return ui_format

    @staticmethod
    def validate_workflow(workflow_data: Dict) -> bool:
        """Basic validation that workflow contains important nodes"""
        if not workflow_data:
            return False
            
        has_output_node = False
        has_checkpoint = False
        
        output_node_types = ["SaveImage", "PreviewImage", "SaveVideo"]
        checkpoint_types = ["CheckpointLoaderSimple", "CheckpointLoader", "UNETLoader"]
        
        for node_id, node in workflow_data.items():
            if not isinstance(node, dict) or "class_type" not in node:
                continue
                
            class_type = node.get("class_type", "")
            
            if class_type in output_node_types:
                has_output_node = True
                
            if class_type in checkpoint_types:
                has_checkpoint = True
                
        if not has_output_node:
            print("Deadline Submission: Warning - No output nodes found in workflow.")
            
        if not has_checkpoint:
            print("Deadline Submission: Warning - No checkpoint loader found in workflow.")
            
        return True

    @staticmethod
    def save_workflow_file(workflow_data: Dict, file_path: Optional[str] = None) -> Optional[str]:
        """Save workflow data to a file for submission"""
        if not workflow_data:
            print("Deadline Submission: No workflow data to save.")
            return None
            
        if not file_path:
            temp_dir = tempfile.gettempdir()
            file_path = os.path.join(temp_dir, f"comfyui_workflow_for_deadline_{uuid.uuid4()}.json")
        
        try:
            with open(file_path, 'w') as f:
                json.dump(workflow_data, f, indent=2)
                
            # Create a metadata file for debugging
            WorkflowProcessor._create_metadata_file(file_path)
                
            print(f"Deadline Submission: Successfully saved workflow for submission to: {file_path}")
            return file_path
        except Exception as e:
            print(f"Deadline Submission: Error saving workflow file: {e}")
            return None

    @staticmethod
    def _create_metadata_file(workflow_path: str):
        """Create a metadata file alongside the workflow"""
        try:
            with open(f"{workflow_path}.metadata", 'w') as f:
                metadata = {
                    "generator": "ComfyUI Deadline Submission Plugin",
                    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "notes": "This workflow was captured and prepared for Deadline rendering."
                }
                json.dump(metadata, f, indent=2)
        except Exception as e:
            print(f"Deadline Submission: Warning - Could not create metadata file: {e}")

    @staticmethod
    def prepare_workflow_for_submission(workflow_data: Dict) -> Dict:
        """Prepare workflow by setting DeadlineSubmit nodes to bypassed"""
        normalized_workflow = WorkflowProcessor.normalize_workflow(workflow_data)
        if not normalized_workflow:
            raise Exception("Failed to normalize workflow")
            
        # Set any DeadlineSubmit nodes to bypassed
        deadline_node_types = ["DeadlineSubmit", "SaveAndSubmitNode"]
        for node_id, node in normalized_workflow.items():
            if isinstance(node, dict) and node.get("class_type") in deadline_node_types:
                print(f"Deadline Submission: Setting node {node_id} to bypassed")
                if "inputs" not in node:
                    node["inputs"] = {}
                node["inputs"]["bypass"] = True
        
        WorkflowProcessor.validate_workflow(normalized_workflow)
        return normalized_workflow

class DeadlineJobSubmitter:
    """Handles submission of jobs to Deadline"""
    
    def __init__(self, workflow_data: Dict, job_config: Dict):
        self.workflow_data = workflow_data
        self.job_config = job_config

    def submit_job(self) -> Tuple[bool, str]:
        """Submit the job to Deadline and return success status and job ID or error message"""
        try:
            workflow_path = self._save_workflow()
            if not workflow_path:
                return False, "Failed to save workflow for submission"
            
            job_id = self._submit_to_deadline(workflow_path)
            if job_id:
                return True, job_id
            else:
                return False, "Job submitted but no JobID returned"
                
        except Exception as e:
            return False, f"Error submitting to Deadline: {str(e)}"

    def _save_workflow(self) -> Optional[str]:
        """Save the workflow to a temporary file"""
        return WorkflowProcessor.save_workflow_file(self.workflow_data)

    def _submit_to_deadline(self, workflow_path: str) -> str:
        """Submit the workflow to Deadline and return job ID"""
        submission_temp_dir = tempfile.mkdtemp(prefix="comfy_deadline_job_")
        
        try:
            job_info_file, plugin_info_file, workflow_copy = self._create_submission_files(
                submission_temp_dir, workflow_path
            )
            
            command_args = [job_info_file, plugin_info_file, workflow_copy]
            result = DeadlineCommandHelper.call_deadline_command(command_args)
            
            job_id = DeadlineCommandHelper.get_job_id_from_submission(result)
            if job_id:
                print(f"Deadline Submission: Successfully submitted job. JobID: {job_id}")
                return job_id
            else:
                print(f"Deadline Submission: Job submitted but JobID not found. Result: {result}")
                return ""
                
        except Exception as e:
            print(f"Deadline Submission: Error during submission: {e}")
            raise

    def _create_submission_files(self, temp_dir: str, workflow_path: str) -> Tuple[str, str, str]:
        """Create job info and plugin info files for submission"""
        job_info_file = os.path.join(temp_dir, "job_info.txt")
        plugin_info_file = os.path.join(temp_dir, "plugin_info.txt")
        
        # Copy workflow to submission directory
        workflow_copy = os.path.join(temp_dir, "workflow_to_submit.json")
        try:
            import shutil
            shutil.copy2(workflow_path, workflow_copy)
        except Exception:
            workflow_copy = workflow_path

        self._create_job_info_file(job_info_file)
        self._create_plugin_info_file(plugin_info_file)
        
        return job_info_file, plugin_info_file, workflow_copy

    def _create_job_info_file(self, job_info_file: str):
        """Create the job info file"""
        config = self.job_config
        
        with open(job_info_file, 'w') as f:
            f.write(f"Plugin=ComfyUI\n")
            f.write(f"Name={config['job_name']}\n")
            f.write(f"Comment={config.get('comment', '')}\n")
            f.write(f"Department={config.get('department', '')}\n")
            f.write(f"Pool={config['pool'] if config['pool'] != 'none' else ''}\n")
            f.write(f"Group={config['group'] if config['group'] != 'none' else ''}\n")
            f.write(f"Priority={config['priority']}\n")
            
            # Add frame range if batch count > 1
            if config['batch_count'] > 1:
                f.write(f"Frames=0-{config['batch_count'] - 1}\n")
                f.write(f"ChunkSize={config['chunk_size']}\n")
            else:
                f.write(f"Frames=0\n")
                f.write(f"ChunkSize=1\n")
            
            # Add output directory if specified
            if config.get('output_directory'):
                abs_output_dir = os.path.abspath(config['output_directory'].strip())
                f.write(f"OutputDirectory0={abs_output_dir}\n")

    def _create_plugin_info_file(self, plugin_info_file: str):
        """Create the plugin info file"""
        config = self.job_config
        
        with open(plugin_info_file, 'w') as f:
            if config.get('output_directory'):
                abs_output_dir = os.path.abspath(config['output_directory'].strip())
                f.write(f"JobOutputDirectory={abs_output_dir}\n")
            
            f.write("DefaultCudaDeviceZero=True\n")
            
            # Map boolean to appropriate SeedMode value
            if config.get('change_seeds_per_task', True):
                f.write("SeedMode=change\n")
            else:
                f.write("SeedMode=fixed\n")
            
            if config['batch_count'] > 1:
                f.write("BatchMode=True\n")

class ExecutionInterruptor:
    """Handles interrupting local ComfyUI execution"""
    
    @staticmethod
    def interrupt_local_execution():
        """Attempt to interrupt local ComfyUI execution"""
        try:
            import sys
            
            # Try to interrupt using the known working approach
            if ExecutionInterruptor._try_nodes_interrupt():
                print("Deadline Submission: Successfully interrupted via nodes module")
            elif ExecutionInterruptor._try_comfy_graph_interrupt():
                print("Deadline Submission: Successfully interrupted via comfy.graph module")
            else:
                print("Deadline Submission: No interruption mechanism found, local execution may still occur")
                
        except Exception as e:
            print(f"Deadline Submission: Unable to prevent local execution (safe to ignore): {str(e)}")

    @staticmethod
    def _try_nodes_interrupt() -> bool:
        """Try to interrupt using the nodes module"""
        import sys
        
        if 'nodes' not in sys.modules:
            return False
            
        nodes_module = sys.modules['nodes']
        if not hasattr(nodes_module, 'interrupt_processing'):
            return False
            
        interrupt_attr = getattr(nodes_module, 'interrupt_processing')
        if callable(interrupt_attr):
            interrupt_attr(True)
        else:
            nodes_module.interrupt_processing = True
            
        return True

    @staticmethod
    def _try_comfy_graph_interrupt() -> bool:
        """Try to interrupt using the comfy.graph module"""
        import sys
        
        if 'comfy' not in sys.modules:
            return False
            
        comfy_module = sys.modules['comfy']
        if not hasattr(comfy_module, 'graph'):
            return False
            
        graph_module = comfy_module.graph
        if not hasattr(graph_module, 'interrupt_processing'):
            return False
            
        interrupt_attr = getattr(graph_module, 'interrupt_processing')
        if callable(interrupt_attr):
            interrupt_attr(True)
        else:
            graph_module.interrupt_processing = True
            
        return True

# Node implementation
class DeadlineSubmitNode:
    """Submit the current ComfyUI workflow to Thinkbox Deadline"""
    
    @classmethod
    def INPUT_TYPES(cls):
        pools = cls._get_deadline_pools()
        groups = cls._get_deadline_groups()
        
        return {
            "required": {
                "workflow_file": ("STRING", {
                    "default": "", 
                    "multiline": False, 
                    "placeholder": "(Optional) Override if auto-detect is OFF"
                }),
                "auto_detect_workflow": ("BOOLEAN", {
                    "default": True, 
                    "label_on": "Use current (recommended)", 
                    "label_off": "Use 'workflow_file' input"
                }),
                "batch_count": ("INT", {
                    "default": NodeDefaults.BATCH_COUNT, 
                    "min": 1, 
                    "max": NodeDefaults.MAX_BATCH_COUNT, 
                    "step": 1
                }),
                "chunk_size": ("INT", {
                    "default": NodeDefaults.CHUNK_SIZE, 
                    "min": 1, 
                    "max": NodeDefaults.MAX_CHUNK_SIZE, 
                    "step": 1
                }),
                "change_seeds_per_task": ("BOOLEAN", {
                    "default": True, 
                    "label_on": "Vary seeds across tasks", 
                    "label_off": "Keep original seeds"
                }),
                "priority": ("INT", {
                    "default": NodeDefaults.PRIORITY, 
                    "min": 0, 
                    "max": NodeDefaults.MAX_PRIORITY
                }),
                "pool": (pools, {"default": NodeDefaults.POOL}),
                "group": (groups, {"default": NodeDefaults.GROUP}),
                "job_name": ("STRING", {"default": NodeDefaults.JOB_NAME}),
                "bypass": ("BOOLEAN", {"default": False}),
                "skip_local_execution": ("BOOLEAN", {
                    "default": True, 
                    "label_on": "Submit Only", 
                    "label_off": "Submit and Run Locally"
                }),
            },
            "optional": {
                "output_directory": ("STRING", {
                    "default": "", 
                    "multiline": False, 
                    "placeholder": "(Optional) Output directory on worker"
                }),
                "comment": ("STRING", {"default": ""}),
                "department": ("STRING", {"default": ""}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "submit_to_deadline"
    CATEGORY = "deadline"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        """Return a unique value each time to force execution"""
        return f"deadline_submit_{time.time()}"

    @classmethod
    def _get_deadline_pools(cls) -> List[str]:
        """Get available Deadline pools"""
        try:
            result = DeadlineCommandHelper.call_deadline_command(["-pools"], hide_window=True)
            pools = [line.strip() for line in result.splitlines() if line.strip()]
            return pools if pools else [NodeDefaults.POOL]
        except Exception as e:
            print(f"Deadline Submission: Error getting Deadline pools: {e}")
            return [NodeDefaults.POOL]

    @classmethod
    def _get_deadline_groups(cls) -> List[str]:
        """Get available Deadline groups"""
        try:
            result = DeadlineCommandHelper.call_deadline_command(["-groups"], hide_window=True)
            groups = [line.strip() for line in result.splitlines() if line.strip()]
            return groups if groups else [NodeDefaults.GROUP]
        except Exception as e:
            print(f"Deadline Submission: Error getting Deadline groups: {e}")
            return [NodeDefaults.GROUP]

    def submit_to_deadline(self, workflow_file, auto_detect_workflow, batch_count, chunk_size, 
                         change_seeds_per_task, priority, pool, group, job_name, bypass, 
                         skip_local_execution=True, output_directory="", comment="", department="", 
                         prompt=None, extra_pnginfo=None):
        """Submit the workflow to Deadline for rendering"""
        if bypass:
            print("Deadline Submission: Bypass enabled. Submission skipped.")
            return ("Bypassed",)
            
        print(f"Deadline Submission: Node execution triggered. Auto-detect: {auto_detect_workflow}")
        
        try:
            # Get workflow data
            workflow_data = self._get_workflow_data(auto_detect_workflow, workflow_file, prompt)
            
            # Prepare workflow for submission
            prepared_workflow = WorkflowProcessor.prepare_workflow_for_submission(workflow_data)
            
            # Create job configuration
            job_config = self._create_job_config(
                job_name, priority, pool, group, batch_count, chunk_size,
                change_seeds_per_task, output_directory, comment, department
            )
            
            # Submit to Deadline
            submitter = DeadlineJobSubmitter(prepared_workflow, job_config)
            success, result = submitter.submit_job()
            
            if success:
                if skip_local_execution:
                    ExecutionInterruptor.interrupt_local_execution()
                return (result,)
            else:
                return (f"Error: {result}",)
                
        except Exception as e:
            print(f"Deadline Submission: Error during submission: {e}")
            return (f"Error: {str(e)}",)

    def _get_workflow_data(self, auto_detect_workflow: bool, workflow_file: str, prompt) -> Dict:
        """Get workflow data from either auto-detection or file"""
        if auto_detect_workflow:
            print("Deadline Submission: Auto-detect ON. Checking for workflow...")
            
            if prompt is None:
                raise Exception("ComfyUI did not inject PROMPT parameter")
                
            print("Deadline Submission: Found workflow from ComfyUI's PROMPT parameter injection")
            return prompt
        else:
            print(f"Deadline Submission: Auto-detect OFF. Using specified workflow_file: '{workflow_file}'.")
            user_workflow_path = workflow_file.strip()
            
            if not user_workflow_path or not os.path.exists(user_workflow_path):
                raise Exception(f"Specified workflow file not found: '{user_workflow_path}'")
                
            try:
                with open(user_workflow_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                raise Exception(f"Could not read workflow file: {str(e)}")

    def _create_job_config(self, job_name: str, priority: int, pool: str, group: str, 
                          batch_count: int, chunk_size: int, change_seeds_per_task: bool,
                          output_directory: str, comment: str, department: str) -> Dict:
        """Create job configuration dictionary"""
        return {
            'job_name': job_name,
            'priority': priority,
            'pool': pool,
            'group': group,
            'batch_count': batch_count,
            'chunk_size': chunk_size,
            'change_seeds_per_task': change_seeds_per_task,
            'output_directory': output_directory,
            'comment': comment,
            'department': department
        }

# Register the nodes
NODE_CLASS_MAPPINGS = {
    "DeadlineSubmit": DeadlineSubmitNode,
}

# Add display names for the nodes
NODE_DISPLAY_NAME_MAPPINGS = {
    "DeadlineSubmit": "Submit to Deadline",
} 