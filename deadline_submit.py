"""
ComfyUI Deadline submission nodes.

This module owns the ComfyUI-side submission flow. It packages the current API
prompt, stages referenced input assets, and submits a render job to Deadline.
"""

import copy
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


DEADLINE_COMMAND_PATHS = {
    "windows": "C:\\Program Files\\Thinkbox\\Deadline10\\bin\\deadlinecommand.exe",
    "linux": "/opt/Thinkbox/Deadline10/bin/deadlinecommand",
}

DEADLINE_SUBMIT_NODE_TYPES = {"DeadlineSubmit", "SaveAndSubmitNode"}
OUTPUT_NODE_TYPES = {"SaveImage", "PreviewImage", "SaveVideo", "VHS_VideoCombine"}
INPUT_LOADER_FIELDS = {
    "LoadImage": ("image",),
    "LoadImageMask": ("image",),
    "LoadAudio": ("audio",),
    "LoadVideo": ("file",),
}
MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".exr",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
    ".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac",
}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class NodeDefaults:
    JOB_NAME = "ComfyUI via Deadline"
    PRIORITY = 50
    POOL = "none"
    GROUP = "none"
    BATCH_COUNT = 1
    CHUNK_SIZE = 1
    MAX_BATCH_COUNT = 10000
    MAX_CHUNK_SIZE = 256
    MAX_PRIORITY = 100


class DeadlineCommandHelper:
    @staticmethod
    def get_deadline_command() -> str:
        deadline_bin = os.environ.get("DEADLINE_PATH", "")

        if not deadline_bin and os.path.exists("/Users/Shared/Thinkbox/DEADLINE_PATH"):
            try:
                with open("/Users/Shared/Thinkbox/DEADLINE_PATH", "r", encoding="utf-8") as handle:
                    deadline_bin = handle.read().strip()
            except Exception:
                deadline_bin = ""

        candidates = []
        if deadline_bin:
            candidates.append(os.path.join(deadline_bin, "deadlinecommand.exe" if os.name == "nt" else "deadlinecommand"))
        candidates.append(DEADLINE_COMMAND_PATHS["windows"] if sys.platform.startswith("win") else DEADLINE_COMMAND_PATHS["linux"])

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return ""

    @staticmethod
    def call_deadline_command(arguments: List[str], hide_window: bool = True) -> str:
        deadline_command = DeadlineCommandHelper.get_deadline_command()
        if not deadline_command:
            raise RuntimeError("Deadline command not found. Set DEADLINE_PATH or install Deadline Client.")

        startupinfo = None
        creationflags = 0
        if os.name == "nt" and hide_window:
            try:
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            except Exception:
                startupinfo = None
        elif os.name == "nt":
            creationflags = 0x08000000

        process = subprocess.Popen(
            [deadline_command] + arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        stdout, stderr = process.communicate()
        output = stdout.decode(errors="replace") if isinstance(stdout, bytes) else str(stdout)
        errors = stderr.decode(errors="replace") if isinstance(stderr, bytes) else str(stderr)

        if process.returncode != 0:
            raise RuntimeError(f"deadlinecommand failed with code {process.returncode}: {errors or output}")
        return output

    @staticmethod
    def get_job_id_from_submission(submission_results: str) -> str:
        for token in submission_results.replace("\r", "\n").split():
            if token.startswith("JobID="):
                return token.split("=", 1)[1].strip()
        return ""


class WorkflowProcessor:
    @staticmethod
    def normalize_prompt(prompt: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(prompt, dict) or not prompt:
            raise ValueError("ComfyUI did not provide a valid API prompt.")
        return copy.deepcopy(prompt)

    @staticmethod
    def prepare_for_worker(prompt: Dict[str, Any]) -> Dict[str, Any]:
        prepared = WorkflowProcessor.normalize_prompt(prompt)
        removed = []
        for node_id, node in list(prepared.items()):
            if isinstance(node, dict) and node.get("class_type") in DEADLINE_SUBMIT_NODE_TYPES:
                removed.append(node_id)
                del prepared[node_id]

        if removed:
            print(f"Deadline Submission: Removed submit node(s) from worker prompt: {', '.join(map(str, removed))}")

        WorkflowProcessor.validate_worker_prompt(prepared)
        return prepared

    @staticmethod
    def validate_worker_prompt(prompt: Dict[str, Any]) -> None:
        if not prompt:
            raise ValueError("Worker prompt is empty after removing Deadline submit nodes.")

        has_output = any(
            isinstance(node, dict) and node.get("class_type") in OUTPUT_NODE_TYPES
            for node in prompt.values()
        )
        if not has_output:
            print("Deadline Submission: Warning - worker prompt has no known output node.")


class InputAssetStager:
    def __init__(self, output_directory: str, job_name: str, submission_id: str):
        self.output_directory = os.path.abspath(output_directory)
        self.job_name = job_name
        self.submission_id = submission_id

    def stage_referenced_assets(self, prompt: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
        input_dir = self._get_local_input_directory()
        references = self._collect_references(prompt, input_dir)
        staging_dir = self._staging_directory()
        manifest_path = os.path.join(staging_dir, f"deadline_input_manifest_{self.submission_id}.json")

        if not references:
            os.makedirs(staging_dir, exist_ok=True)
            manifest = {
                "submission_id": self.submission_id,
                "input_directory": staging_dir,
                "assets": [],
            }
            self._write_manifest(manifest_path, manifest)
            return staging_dir, manifest_path, []

        os.makedirs(staging_dir, exist_ok=True)
        assets = []
        for original_rel_path, source_path in sorted(references.items()):
            staged_rel_path, destination_path = self._resolve_destination(staging_dir, original_rel_path, source_path)
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            if not os.path.exists(destination_path):
                shutil.copy2(source_path, destination_path)
            assets.append({
                "original_relative_path": original_rel_path.replace("\\", "/"),
                "staged_relative_path": staged_rel_path.replace("\\", "/"),
                "relative_path": staged_rel_path.replace("\\", "/"),
                "source": source_path,
                "destination": destination_path,
                "size": os.path.getsize(destination_path),
            })

        manifest = {
            "submission_id": self.submission_id,
            "input_directory": staging_dir,
            "assets": assets,
        }
        self._write_manifest(manifest_path, manifest)
        print(f"Deadline Submission: Staged {len(assets)} input asset(s) to {staging_dir}")
        return staging_dir, manifest_path, assets

    def _get_local_input_directory(self) -> str:
        try:
            import folder_paths
            return os.path.abspath(folder_paths.get_input_directory())
        except Exception as exc:
            raise RuntimeError(f"Could not resolve ComfyUI input directory: {exc}")

    def _collect_references(self, prompt: Dict[str, Any], input_dir: str) -> Dict[str, str]:
        references: Dict[str, str] = {}
        for node in prompt.values():
            if not isinstance(node, dict):
                continue

            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue

            candidate_values: List[Tuple[Any, bool]] = []
            for field_name in INPUT_LOADER_FIELDS.get(class_type, ()):
                if field_name in inputs:
                    candidate_values.append((inputs[field_name], True))

            for value in inputs.values():
                if isinstance(value, str):
                    candidate_values.append((value, False))

            for value, strict in candidate_values:
                if not isinstance(value, str):
                    continue
                resolved = self._resolve_input_file(value, input_dir, strict)
                if not resolved:
                    continue
                rel_path, source_path = resolved
                references[rel_path] = source_path
        return references

    def _resolve_destination(self, staging_dir: str, rel_path: str, source_path: str) -> Tuple[str, str]:
        destination_path = os.path.abspath(os.path.join(staging_dir, rel_path))
        if not os.path.exists(destination_path):
            return rel_path, destination_path

        if os.path.isfile(destination_path) and filecmp.cmp(source_path, destination_path, shallow=False):
            return rel_path, destination_path

        stem, extension = os.path.splitext(rel_path)
        staged_rel_path = f"{stem}_{self.submission_id}{extension}"
        return staged_rel_path, os.path.abspath(os.path.join(staging_dir, staged_rel_path))

    def _resolve_input_file(self, value: str, input_dir: str, strict: bool) -> Optional[Tuple[str, str]]:
        clean_value, annotation = self._strip_annotation(value)
        if annotation in {"output", "temp"}:
            return None
        if not clean_value or os.path.isabs(clean_value):
            return None
        if os.path.splitext(clean_value)[1].lower() not in MEDIA_EXTENSIONS:
            return None

        candidate = os.path.abspath(os.path.join(input_dir, clean_value))
        try:
            common = os.path.commonpath([input_dir, candidate])
        except ValueError:
            common = ""
        if common != input_dir:
            raise ValueError(f"Input asset escapes ComfyUI input directory: {value}")
        if not os.path.isfile(candidate):
            if not strict:
                return None
            raise FileNotFoundError(f"Referenced input asset was not found: {value} ({candidate})")

        rel_path = os.path.relpath(candidate, input_dir)
        return rel_path, candidate

    def _strip_annotation(self, value: str) -> Tuple[str, Optional[str]]:
        match = re.match(r"^(.*)\s+\[(input|output|temp)\]\s*$", value)
        if not match:
            return value.strip().replace("/", os.sep), None
        return match.group(1).strip().replace("/", os.sep), match.group(2)

    def _staging_directory(self) -> str:
        parent = os.path.dirname(self.output_directory.rstrip("\\/"))
        return os.path.join(parent, "input")

    def _write_manifest(self, manifest_path: str, manifest: Dict[str, Any]) -> None:
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)


class DeadlineJobSubmitter:
    def __init__(self, workflow_data: Dict[str, Any], job_config: Dict[str, Any]):
        self.workflow_data = workflow_data
        self.job_config = job_config

    def submit_job(self) -> Tuple[bool, str]:
        try:
            submission_dir = tempfile.mkdtemp(prefix="comfy_deadline_job_")
            job_info_file, plugin_info_file, auxiliary_files = self._create_submission_files(submission_dir)
            result = DeadlineCommandHelper.call_deadline_command([job_info_file, plugin_info_file] + auxiliary_files)
            job_id = DeadlineCommandHelper.get_job_id_from_submission(result)
            if not job_id:
                return False, f"Deadline submission did not return a JobID. Output: {result}"
            return True, job_id
        except Exception as exc:
            return False, str(exc)

    def _create_submission_files(self, submission_dir: str) -> Tuple[str, str, List[str]]:
        job_info_file = os.path.join(submission_dir, "job_info.txt")
        plugin_info_file = os.path.join(submission_dir, "plugin_info.txt")
        prompt_file = os.path.join(submission_dir, "prompt_to_execute.json")
        standard_workflow_file = os.path.join(submission_dir, "workflow.json")

        with open(prompt_file, "w", encoding="utf-8") as handle:
            json.dump(self.workflow_data, handle, indent=2)

        auxiliary_files = [prompt_file]
        standard_workflow = self.job_config.get("standard_workflow")
        if standard_workflow:
            with open(standard_workflow_file, "w", encoding="utf-8") as handle:
                json.dump(standard_workflow, handle, indent=2)
            auxiliary_files.append(standard_workflow_file)

        self._write_job_info(job_info_file)
        self._write_plugin_info(plugin_info_file)
        return job_info_file, plugin_info_file, auxiliary_files

    def _write_job_info(self, path: str) -> None:
        config = self.job_config
        batch_count = int(config["batch_count"])
        chunk_size = max(1, int(config["chunk_size"]))
        output_dir = os.path.abspath(config["output_directory"])

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Plugin=ComfyUI\n")
            handle.write(f"Name={config['job_name']}\n")
            handle.write(f"Comment={config.get('comment', '')}\n")
            handle.write(f"Department={config.get('department', '')}\n")
            handle.write(f"Pool={'' if config['pool'] == 'none' else config['pool']}\n")
            handle.write(f"Group={'' if config['group'] == 'none' else config['group']}\n")
            handle.write(f"Priority={int(config['priority'])}\n")
            handle.write(f"Frames=0-{batch_count - 1}\n")
            handle.write(f"ChunkSize={chunk_size}\n")
            handle.write(f"OutputDirectory0={output_dir}\n")

    def _write_plugin_info(self, path: str) -> None:
        config = self.job_config
        entries = {
            "StandardWorkflowFile": "workflow.json" if config.get("standard_workflow") else "",
            "JobOutputDirectory": os.path.abspath(config["output_directory"]),
            "JobInputDirectory": os.path.abspath(config["input_directory"]),
            "InputManifestFile": os.path.abspath(config["input_manifest"]),
            "SubmissionId": config["submission_id"],
            "BatchCount": str(int(config["batch_count"])),
            "BatchMode": "True",
            "DefaultCudaDeviceZero": "True",
            "SeedMode": "fixed",
            "WorkerMode": "False",
            "DistributedMode": "False",
            "ForceNewInstance": "True",
        }

        with open(path, "w", encoding="utf-8") as handle:
            for key, value in entries.items():
                if value != "":
                    handle.write(f"{key}={value}\n")


class DeadlineSeed:
    """
    Deadline-compatible seed node.
    Batch mode varies by Deadline task ID; distributed mode varies by worker ID.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": ("INT", {
                    "default": 1125899906842,
                    "min": 0,
                    "max": 1125899906842624,
                    "forceInput": False,
                }),
            },
            "hidden": {
                "task_id": ("INT", {"default": 0}),
                "batch_mode": ("BOOLEAN", {"default": False}),
                "is_worker": ("BOOLEAN", {"default": False}),
                "worker_id": ("STRING", {"default": ""}),
            },
        }

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("seed",)
    FUNCTION = "distribute"
    CATEGORY = "deadline"

    def distribute(self, seed, task_id=0, batch_mode=False, is_worker=False, worker_id=""):
        seed = int(seed)

        if _coerce_bool(batch_mode):
            try:
                task_id = int(task_id)
            except (TypeError, ValueError):
                task_id = 0
            return (seed + task_id,)

        if _coerce_bool(is_worker):
            try:
                worker_id = str(worker_id)
                if worker_id.startswith("worker_"):
                    worker_index = int(worker_id.split("_")[1])
                else:
                    worker_index = int(worker_id)
                return (seed + worker_index + 1,)
            except (TypeError, ValueError, IndexError):
                return (seed,)

        return (seed,)


class LegacyDeadlineSeedAlias(DeadlineSeed):
    DEPRECATED = True


class DeadlineSubmitNode:
    @classmethod
    def INPUT_TYPES(cls):
        pools = cls._get_deadline_pools()
        groups = cls._get_deadline_groups()
        return {
            "required": {
                "output_directory": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "Farm-visible output directory",
                }),
                "batch_count": ("INT", {
                    "default": NodeDefaults.BATCH_COUNT,
                    "min": 1,
                    "max": NodeDefaults.MAX_BATCH_COUNT,
                    "step": 1,
                }),
                "chunk_size": ("INT", {
                    "default": NodeDefaults.CHUNK_SIZE,
                    "min": 1,
                    "max": NodeDefaults.MAX_CHUNK_SIZE,
                    "step": 1,
                }),
                "priority": ("INT", {
                    "default": NodeDefaults.PRIORITY,
                    "min": 0,
                    "max": NodeDefaults.MAX_PRIORITY,
                }),
                "pool": (pools, {"default": NodeDefaults.POOL}),
                "group": (groups, {"default": NodeDefaults.GROUP}),
                "job_name": ("STRING", {"default": NodeDefaults.JOB_NAME}),
            },
            "optional": {
                "comment": ("STRING", {"default": ""}),
                "department": ("STRING", {"default": ""}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("job_id",)
    FUNCTION = "submit_to_deadline"
    CATEGORY = "deadline"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return f"deadline_submit_{time.time()}_{uuid.uuid4()}"

    @classmethod
    def _get_deadline_pools(cls) -> List[str]:
        try:
            output = DeadlineCommandHelper.call_deadline_command(["-pools"])
            pools = [line.strip() for line in output.splitlines() if line.strip()]
            return pools or [NodeDefaults.POOL]
        except Exception as exc:
            print(f"Deadline Submission: Could not query Deadline pools: {exc}")
            return [NodeDefaults.POOL]

    @classmethod
    def _get_deadline_groups(cls) -> List[str]:
        try:
            output = DeadlineCommandHelper.call_deadline_command(["-groups"])
            groups = [line.strip() for line in output.splitlines() if line.strip()]
            return groups or [NodeDefaults.GROUP]
        except Exception as exc:
            print(f"Deadline Submission: Could not query Deadline groups: {exc}")
            return [NodeDefaults.GROUP]

    def submit_to_deadline(
        self,
        output_directory,
        batch_count,
        chunk_size,
        priority,
        pool,
        group,
        job_name,
        comment="",
        department="",
        prompt=None,
        extra_pnginfo=None,
        **_legacy_inputs,
    ):
        try:
            output_directory = self._prepare_output_directory(output_directory)
            batch_count = max(1, int(batch_count))
            chunk_size = max(1, min(int(chunk_size), batch_count))
            submission_id = uuid.uuid4().hex[:12]

            if prompt is None:
                raise ValueError("ComfyUI did not inject the current API prompt.")

            worker_prompt = WorkflowProcessor.prepare_for_worker(prompt)
            stager = InputAssetStager(output_directory, job_name, submission_id)
            input_dir, manifest_file, assets = stager.stage_referenced_assets(worker_prompt)
            self._rewrite_prompt_asset_references(worker_prompt, assets)
            standard_workflow = self._extract_standard_workflow(extra_pnginfo)
            self._rewrite_standard_workflow_assets(standard_workflow, assets)

            job_config = {
                "submission_id": submission_id,
                "job_name": job_name.strip() or NodeDefaults.JOB_NAME,
                "priority": int(priority),
                "pool": pool,
                "group": group,
                "batch_count": batch_count,
                "chunk_size": chunk_size,
                "output_directory": output_directory,
                "input_directory": input_dir,
                "input_manifest": manifest_file,
                "standard_workflow": standard_workflow,
                "comment": comment,
                "department": department,
            }

            submitter = DeadlineJobSubmitter(worker_prompt, job_config)
            success, result = submitter.submit_job()
            if not success:
                raise RuntimeError(result)

            print(f"Deadline Submission: Submitted job {result} with {batch_count} variation(s), chunk size {chunk_size}, {len(assets)} staged asset(s).")
            return (result,)
        except Exception as exc:
            print(f"Deadline Submission: Error during submission: {exc}")
            raise

    def _rewrite_prompt_asset_references(self, prompt: Dict[str, Any], assets: List[Dict[str, Any]]) -> None:
        staged_by_original = self._asset_map(assets, "staged_relative_path")
        if not staged_by_original:
            return

        for node in prompt.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get("inputs")
            if not isinstance(inputs, dict):
                continue

            class_type = node.get("class_type", "")
            for field_name in INPUT_LOADER_FIELDS.get(class_type, ()):
                value = inputs.get(field_name)
                normalized = self._normalize_asset_reference(value)
                if normalized in staged_by_original:
                    inputs[field_name] = staged_by_original[normalized]

            for field_name, value in list(inputs.items()):
                normalized = self._normalize_asset_reference(value)
                if normalized in staged_by_original:
                    inputs[field_name] = staged_by_original[normalized]

    def _rewrite_standard_workflow_assets(self, workflow: Optional[Dict[str, Any]], assets: List[Dict[str, Any]]) -> None:
        staged_absolute_by_original = self._asset_map(assets, "destination")
        if not workflow or not staged_absolute_by_original:
            return

        nodes = workflow.get("nodes", [])
        if not isinstance(nodes, list):
            return

        for node in nodes:
            if not isinstance(node, dict):
                continue
            widgets = node.get("widgets_values")
            if not isinstance(widgets, list):
                continue

            for index, value in enumerate(widgets):
                normalized = self._normalize_asset_reference(value)
                if normalized in staged_absolute_by_original:
                    widgets[index] = staged_absolute_by_original[normalized]

    def _asset_map(self, assets: List[Dict[str, Any]], target_key: str) -> Dict[str, str]:
        mapping = {}
        for asset in assets:
            original = self._normalize_asset_reference(asset.get("original_relative_path"))
            target = asset.get(target_key)
            if original and target:
                mapping[original] = target
        return mapping

    def _normalize_asset_reference(self, value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value:
            return None
        cleaned = re.sub(r"\s+\[(input|output|temp)\]\s*$", "", value.strip())
        if os.path.isabs(cleaned):
            return None
        return os.path.normpath(cleaned.replace("/", os.sep)).replace("\\", "/")

    def _extract_standard_workflow(self, extra_pnginfo: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(extra_pnginfo, dict):
            return None

        workflow = extra_pnginfo.get("workflow")
        if isinstance(workflow, dict):
            return copy.deepcopy(workflow)
        if isinstance(workflow, str):
            try:
                parsed = json.loads(workflow)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
        return None

    def _prepare_output_directory(self, output_directory: str) -> str:
        output_directory = (output_directory or "").strip().strip("\"")
        if not output_directory:
            raise ValueError("output_directory is required and must be farm-visible.")

        output_directory = os.path.abspath(os.path.expandvars(output_directory))
        os.makedirs(output_directory, exist_ok=True)
        if not os.path.isdir(output_directory):
            raise ValueError(f"Output path is not a directory: {output_directory}")
        return output_directory


def on_prompt(json_data: Dict[str, Any]) -> Dict[str, Any]:
    prompt = json_data.get("prompt")
    if not isinstance(prompt, dict):
        return json_data

    submit_ids = [
        str(node_id)
        for node_id, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") in DEADLINE_SUBMIT_NODE_TYPES
    ]

    if submit_ids:
        json_data["partial_execution_targets"] = submit_ids[:1]
        if len(submit_ids) > 1:
            print(f"Deadline Submission: Multiple submit nodes detected; only node {submit_ids[0]} will execute locally.")
    return json_data


def register_on_prompt_handler() -> None:
    try:
        import server
        instance = getattr(getattr(server, "PromptServer", None), "instance", None)
        if instance and hasattr(instance, "add_on_prompt_handler"):
            instance.add_on_prompt_handler(on_prompt)
            print("Deadline Submission: Registered submit-only prompt handler.")
    except Exception as exc:
        print(f"Deadline Submission: Could not register prompt handler: {exc}")


NODE_CLASS_MAPPINGS = {
    "DeadlineSubmit": DeadlineSubmitNode,
    "DeadlineSeed": DeadlineSeed,
    "DeadlineDistributedSeed": LegacyDeadlineSeedAlias,
    "DistributedSeed": LegacyDeadlineSeedAlias,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DeadlineSubmit": "Submit to Deadline",
    "DeadlineSeed": "Deadline Seed",
    "DeadlineDistributedSeed": "Deadline Seed",
    "DistributedSeed": "Deadline Seed",
}
