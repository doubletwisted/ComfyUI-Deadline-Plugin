"""Durable Deadline submission and a cached provider for ComfyUI's native Jobs.

All filesystem/Deadline work runs outside aiohttp's event loop. The share is
authoritative; this provider's cache can be discarded and rebuilt on restart.
"""
import asyncio
import copy
import getpass
import json
import mimetypes
import os
import re
import threading
import time
import uuid
import shutil
from urllib.parse import urlencode
from pathlib import Path

from aiohttp import web

try:
    from .deadline_submit import DeadlineCommandHelper, DeadlineSubmitNode, InputAssetStager
    from .farm_protocol import RunStore, SCHEMA, TERMINAL, atomic_json, canonical_id, digest, read_json, record_lock, safe_path
    from .native_jobs_adapter import NativeJobsAdapter
    from .farm_readiness import off_host_root, readiness as evaluate_readiness
except ImportError:
    from deadline_submit import DeadlineCommandHelper, DeadlineSubmitNode, InputAssetStager
    from farm_protocol import RunStore, SCHEMA, TERMINAL, atomic_json, canonical_id, digest, read_json, record_lock, safe_path
    from native_jobs_adapter import NativeJobsAdapter
    from farm_readiness import off_host_root, readiness as evaluate_readiness

DEFAULTS = {"enabled": False, "root": "", "scope": getpass.getuser(), "pool": "none", "group": "none",
    "priority": 50, "workers": "", "machine_limit": 1, "task_timeout_minutes": 30, "plugin_directory": ""}
SETTINGS_KEYS = set(DEFAULTS)
DISTRIBUTED_NODE_TYPES = {"DistributedCollector", "DeadlineDistributedCollector", "DistributedVideoCollector",
    "DeadlineDistributedVideoCollector", "UltimateSDUpscaleDistributed", "DeadlineUltimateSDUpscaleDistributed"}


class SettingsConflict(ValueError):
    pass


def field(value):
    value = str(value)
    if any(c in value for c in "\r\n\0"):
        raise ValueError("Deadline fields cannot contain newlines or NUL")
    return value


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - SETTINGS_KEYS:
        raise ValueError("Unknown Deadline setting")
    settings = {**DEFAULTS, **value}
    if type(settings["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    for key in ("scope", "pool", "group", "workers", "root", "plugin_directory"):
        settings[key] = field(settings[key]).strip()
    if not settings["scope"] or len(settings["scope"]) > 120:
        raise ValueError("A user scope of 1 to 120 characters is required")
    for key, low, high in (("priority", 0, 100), ("machine_limit", 1, 10000), ("task_timeout_minutes", 1, 1440)):
        if type(settings[key]) is not int or not low <= settings[key] <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if settings["enabled"] and not off_host_root(settings["root"]):
        raise ValueError("Native queue requires an off-host UNC root")
    if settings["enabled"] and settings["root"].split("\\")[2].lower() in {"localhost", "127.0.0.1", os.environ.get("COMPUTERNAME", "").lower()}:
        raise ValueError("Native queue storage must be off the submitting host")
    if settings["plugin_directory"] and not off_host_root(settings["plugin_directory"]):
        raise ValueError("Custom plugin directory must be an off-host UNC folder containing ComfyUI")
    return settings


def parse_ini(output):
    records, current = {}, None
    for line in output.splitlines():
        if line.startswith("[") and line.endswith("]"):
            current = records.setdefault(line[1:-1], {})
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    return records


def deadline(*args):
    result = DeadlineCommandHelper.call_deadline_command(list(args), timeout=30)
    if any(line.startswith("Error:") for line in result.splitlines()):
        raise RuntimeError(result.strip())
    return result


class FarmBackend:
    def __init__(self, settings_path=None, command=deadline):
        self.settings_path = Path(settings_path or os.environ.get("COMFY_DEADLINE_NATIVE_SETTINGS") or Path(__file__).parent / "deadline_native_settings.json")
        self.command = command
        self.plugin_directory = field(os.environ.get("COMFY_DEADLINE_NATIVE_PLUGIN_DIR", ""))
        self.lock = threading.RLock()
        self.configuration_lock = threading.RLock()
        self.configuration_in_progress = False
        self.active_submissions = 0
        configuration_error = None
        try:
            self.settings = validate_settings(read_json(self.settings_path)) if self.settings_path.exists() else dict(DEFAULTS)
        except (OSError, ValueError, TypeError) as exc:
            self.settings = dict(DEFAULTS)
            configuration_error = "Native settings could not be loaded: " + str(exc)
        self.jobs, self.histories, self.assets, self.progress = {}, {}, {}, []
        self.health = {"connected": False, "message": configuration_error or "Native queue is disabled; configure an off-host root and Workers.", "last_refresh": None}
        self.wakeup = threading.Event()
        self.shutdown = threading.Event()
        self.thread = None
        self.result_cache = {}
        self.discovery_lock = threading.Lock()
        self.discovery_checked = 0
        self.discovery_plugin_directory = None
        self.discovery = {"connected": False, "message": "Not checked yet", "workers": [], "pools": [], "groups": [], "last_refresh": None}
        self.readiness_cache = evaluate_readiness(self.settings, self.discovery)
        try:
            import folder_paths
            self.output_root = Path(folder_paths.get_output_directory())
        except (ImportError, AttributeError):
            self.output_root = None

    def _effective_plugin_directory(self, settings):
        return settings["plugin_directory"] or self.plugin_directory

    def discover(self, force=False, plugin_directory=None):
        if plugin_directory is None:
            with self.lock:
                plugin_directory = self._effective_plugin_directory(self.settings)
        requested = time.monotonic()
        with self.discovery_lock:
            same_plugin = self.discovery_plugin_directory == plugin_directory
            if same_plugin and self.discovery_checked and (requested - self.discovery_checked < 15 and not force or self.discovery_checked > requested):
                return copy.deepcopy(self.discovery)
            try:
                arguments = ["-ExecuteScript", str(Path(__file__).parent / "scripts" / "deadline_farm_snapshot.py")]
                if plugin_directory:
                    arguments.append(plugin_directory)
                output = self.command(*arguments)
                lines = [line[len("COMFY_NATIVE_FARM="):] for line in output.splitlines() if line.startswith("COMFY_NATIVE_FARM=")]
                if len(lines) != 1:
                    raise ValueError("Deadline did not return a farm snapshot")
                snapshot = json.loads(lines[0])
                if not isinstance(snapshot, dict) or any(not isinstance(snapshot.get(key), list) for key in ("workers", "pools", "groups")):
                    raise ValueError("Invalid Deadline farm snapshot")
                snapshot["deadline_user"] = field(self.command("-GetCurrentUserName").strip())
                self.discovery = {**snapshot, "connected": True, "message": "Repository connected", "last_refresh": int(time.time() * 1000)}
            except Exception as exc:
                self.discovery.update(connected=False, message=str(exc))
                if not same_plugin:
                    self.discovery.update(plugin_available=False, plugin_directory=plugin_directory)
            self.discovery_checked = time.monotonic()
            self.discovery_plugin_directory = plugin_directory
            return copy.deepcopy(self.discovery)

    def readiness(self, settings=None, force=False):
        if settings is None:
            with self.lock:
                settings = dict(self.settings)
        else:
            settings = validate_settings(settings)
        snapshot = self.discover(force, self._effective_plugin_directory(settings))
        storage = {"connected": False, "message": "Choose an off-host shared folder."}
        if off_host_root(settings["root"]):
            try:
                RunStore(settings["root"]).check_root()
                storage = {"connected": True, "message": "Shared storage identity is available."}
            except (OSError, ValueError, RuntimeError) as exc:
                storage["message"] = str(exc)
        result = evaluate_readiness(settings, {**snapshot, "storage": storage})
        with self.lock:
            if settings == self.settings:
                self.readiness_cache = result
        return result

    def _require_ready(self, settings):
        result = self.readiness(settings)
        if not result["ready"]:
            if result["code"] in {"storage", "repository"}:
                raise RuntimeError(result["message"])
            raise ValueError(result["message"])
        return result

    def configure(self, value, expected=None):
        with self.configuration_lock:
            return self._configure(value, expected)

    def _configure(self, value, expected=None):
        settings = validate_settings(value)
        with self.lock:
            if expected is not None and expected != self.settings:
                raise SettingsConflict("Settings changed elsewhere. Revert to reload before applying.")
            self._check_storage_change(settings)
            previous = dict(self.settings)
            changing_store = (settings["root"], settings["scope"]) != (previous["root"], previous["scope"])
            if changing_store:
                self.configuration_in_progress = True
        try:
            if changing_store:
                self._check_durable_runs(previous)
            return self._write_configuration(settings, expected)
        finally:
            if changing_store:
                with self.lock:
                    self.configuration_in_progress = False

    def _write_configuration(self, settings, expected):
        initializing = settings["enabled"] and (not self.settings["enabled"] or
            (settings["root"], settings["scope"]) != (self.settings["root"], self.settings["scope"]))
        if initializing:
            root = Path(settings["root"])
            root.mkdir(parents=True, exist_ok=True)
            with record_lock(root / "health-probe"):
                atomic_json(root / "health-probe" / "probe.json", {"schema": SCHEMA, "time": time.time()})
            self.command("-GetRepositoryPath")
            RunStore(root).initialize()
            ready = self._require_ready(settings)
        else:
            ready = evaluate_readiness(settings, {**self.discovery, "storage":self.health})
        with self.lock:
            if expected is not None and expected != self.settings:
                raise SettingsConflict("Settings changed elsewhere. Revert to reload before applying.")
            self._check_storage_change(settings)
            if (settings["root"], settings["scope"]) != (self.settings["root"], self.settings["scope"]):
                self.jobs, self.histories, self.assets, self.progress = {}, {}, {}, []
                self.result_cache.clear()
            atomic_json(self.settings_path, settings)
            self.settings = settings
            self.readiness_cache = ready if settings["enabled"] else evaluate_readiness(settings, self.discovery)
        self.wakeup.set()
        return settings

    def _check_storage_change(self, settings):
        if (settings["root"], settings["scope"]) != (self.settings["root"], self.settings["scope"]):
            if self.active_submissions or any(j["status"] not in TERMINAL for j in self.jobs.values()):
                raise ValueError("Wait for active submissions and runs before changing the shared root or user scope")

    def _check_durable_runs(self, settings):
        if not settings["root"]:
            return
        if not settings["enabled"] and not off_host_root(settings["root"]):
            # Disabled local/malformed roots cannot contain accepted native runs.
            return
        store = RunStore(settings["root"])
        try:
            store.check_root()
        except RuntimeError:
            # Verify the share is reachable before treating a new folder as empty.
            if not settings["enabled"]:
                directory = store.root
                share = Path(directory.anchor)
                while True:
                    try:
                        entries = {path.name for path in directory.iterdir()}
                    except FileNotFoundError:
                        if directory == share or directory.parent == directory:
                            raise RuntimeError("The configured shared folder is unavailable; reconnect the share before changing storage or updating setup") from None
                        directory = directory.parent
                        continue
                    if directory != store.root or "store.json" not in entries and "runs" not in entries:
                        return
                    break
            raise
        pending = []
        for submission in store.records(settings["scope"]):
            run_id = submission["run_id"]
            control = store.control(run_id)
            if control.get("result") or control["status"] == "cancelled" or control.get("deadline_removed"):
                continue
            receipt_path = store.run(run_id) / "receipt.json"
            receipt = read_json(receipt_path) if receipt_path.exists() else {}
            if not receipt.get("job_id"):
                raise ValueError("Resolve pending or unconfirmed farm runs before changing storage or updating setup: " + run_id)
            pending.append((run_id, receipt["job_id"]))
        remote = {}
        for offset in range(0, len(pending), 100):
            remote.update(parse_ini(self.command("-GetJob", ",".join(job_id for _, job_id in pending[offset:offset + 100]), "True")))
        for run_id, job_id in pending:
            if remote.get(job_id, {}).get("Status") not in {"Completed", "Failed"}:
                raise ValueError("Finish pending or unconfirmed farm runs before changing storage or updating setup: " + run_id)

    def start(self):
        self.thread = threading.Thread(target=self._refresh_loop, name="Deadline-native-reconciler", daemon=True)
        self.thread.start()

    def _refresh_loop(self):
        while not self.shutdown.is_set():
            try:
                if not self.settings_path.exists():
                    site = self.discover()
                    root = os.environ.get("COMFY_DEADLINE_NATIVE_ROOT", "") or site.get("native_root", "")
                    if root and site.get("deadline_user"):
                        self.configure({**self.settings, "root":root, "scope":site["deadline_user"], "enabled":True})
                self.refresh()
                with self.lock:
                    enabled = self.settings["enabled"]
                if enabled:
                    self.readiness()
            except Exception as exc:
                with self.lock:
                    self.health.update(connected=False, message=str(exc))
                    snapshot = {**self.discovery, "storage": self.health}
                    self.readiness_cache = evaluate_readiness(self.settings, snapshot)
            self.wakeup.wait(3)
            self.wakeup.clear()

    def submit(self, body, captured=None, retry_of=None):
        if not isinstance(body, dict) or not isinstance(body.get("prompt"), dict) or not body["prompt"]:
            raise ValueError("A non-empty API prompt is required")
        with self.lock:
            if self.configuration_in_progress:
                raise ValueError("Shared storage configuration is changing. Wait before submitting.")
            settings = validate_settings(captured or {**self.settings, **body.get("settings", {})})
            if not self.settings["enabled"] or not settings["enabled"]:
                raise ValueError("Native Deadline queue is disabled")
            # Retried snapshots and browser captures belong to the current store.
            if any(settings[k] != self.settings[k] for k in ("root", "scope", "enabled")) or (captured is None and settings["plugin_directory"] != self.settings["plugin_directory"]):
                raise ValueError("Change storage settings through the settings endpoint")
            self.active_submissions += 1
        try:
            return self._submit(body, settings, retry_of)
        finally:
            with self.lock:
                self.active_submissions -= 1

    def _submit(self, body, settings, retry_of):
        key = canonical_id(body.get("submission_key"))
        batch = canonical_id(body["batch_id"]) if body.get("batch_id") else str(uuid.uuid4())
        if body.get("partial_execution_targets"):
            raise ValueError("Run (Deadline) supports whole workflows only")
        if any(not isinstance(n, dict) or not isinstance(n.get("inputs"), dict) or not n.get("class_type") for n in body["prompt"].values()):
            raise ValueError("Malformed API prompt node")
        if any(n["class_type"] in {"DeadlineSubmit", "SaveAndSubmitNode"} for n in body["prompt"].values()):
            raise ValueError("Use local Run for workflows containing Submit to Deadline")
        if any(n["class_type"] in DISTRIBUTED_NODE_TYPES for n in body["prompt"].values()):
            raise ValueError("Use local Run for workflows containing Distributed nodes; Deadline queues whole workflows")
        extra = copy.deepcopy(body.get("extra_data", {}))
        extra.pop("auth_token_comfy_org", None)
        extra.pop("api_key_comfy_org", None)
        workflow = extra.get("extra_pnginfo", {}).get("workflow")
        if not isinstance(workflow, dict) or "__deadline_native_queue" in workflow:
            raise ValueError("A readable workflow without routing markers is required")
        payload = {"prompt": copy.deepcopy(body["prompt"]), "extra_data": extra}
        fingerprint = digest({"payload": payload, "settings": settings, "batch_id": body.get("batch_id"), "retry_of": retry_of})
        self._require_ready(settings)
        store = RunStore(settings["root"])
        store.check_root()
        key_dir = store.key(settings["scope"], key)
        with record_lock(key_dir, timeout=35):
            request_path = key_dir / "request.json"
            if request_path.exists():
                request = read_json(request_path)
                if request["hash"] != fingerprint:
                    raise ValueError("Same submission_key with a different snapshot or settings")
                run_id = request["run_id"]
            else:
                run_id = str(uuid.uuid4())
                atomic_json(request_path, {"run_id": run_id, "hash": fingerprint})
            directory = store.run(run_id)
            if not (directory / "submission.json").exists():
                directory.mkdir(parents=True, exist_ok=True)
                source_input = str(store.run(retry_of) / "inputs") if retry_of else None
                stager = InputAssetStager(str(directory / "outputs"), "Native queue", run_id, str(directory / "inputs"), source_input, native_assets=True)
                _input, manifest, assets = stager.stage_referenced_assets(payload["prompt"])
                rewriter = DeadlineSubmitNode()
                rewriter._rewrite_prompt_asset_references(payload["prompt"], assets)
                rewriter._rewrite_standard_workflow_assets(workflow, assets)
                for asset in assets:
                    asset["sha256"] = self._hash_file(Path(asset["destination"]))
                atomic_json(directory / "input_manifest.json", {"schema": SCHEMA, "assets": assets})
                if Path(manifest) != directory / "input_manifest.json":
                    Path(manifest).unlink()
                created = int(time.time() * 1000)
                payload["extra_data"]["create_time"] = created
                submission = {"schema": SCHEMA, "run_id": run_id, "submission_key": key, "batch_id": batch,
                    "scope": settings["scope"], "deadline_user": field(self.command("-GetCurrentUserName").strip()),
                    "created_at": created, "workflow_id": workflow.get("id"),
                    "settings": settings, "payload": payload, "snapshot_hash": digest(payload),
                    "request_hash": fingerprint, "retry_of": retry_of, "name": "Comfy Native " + run_id}
                submission["plugin_directory"] = self._effective_plugin_directory(settings)
                atomic_json(directory / "prompt.json", payload["prompt"])
                atomic_json(directory / "workflow.json", workflow)
                atomic_json(directory / "control.json", {"schema": SCHEMA, "generation": 0, "status": "pending",
                    "cancel_requested": False, "result": None})
                atomic_json(directory / "submission.json", submission)
            submission = read_json(directory / "submission.json")
            with record_lock(directory):
                receipt = self._ensure_submitted(store, submission)
            if not receipt.get("accepted"):
                raise RuntimeError("Submission is unconfirmed; retry this request key to reconcile without duplicating it")
            self._cache_submission(submission, receipt)
        self.wakeup.set()
        return {"prompt_id": run_id, "number": 0, "node_errors": {}}

    def _cache_submission(self, submission, receipt):
        run_id = submission["run_id"]
        with self.lock:
            if run_id in self.jobs:
                return
            self.jobs[run_id] = {"id": run_id, "status": "pending", "create_time": submission["created_at"],
                "workflow_id": submission["workflow_id"], "outputs_count": 0, "previewable_outputs_count": 0,
                "workflow": submission["payload"], "outputs": {}, "deadline": {"job_id": receipt["job_id"], "accepted": True}}
            self.assets[run_id] = []
            self.histories[run_id] = {"prompt": [0, run_id, submission["payload"]["prompt"], submission["payload"]["extra_data"], list(submission["payload"]["prompt"])],
                "outputs": {}, "status": {"completed": False, "status_str": "success", "messages": []}}

    @staticmethod
    def _hash_file(path):
        import hashlib
        value = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(block)
        return value.hexdigest()

    def _ensure_submitted(self, store, submission):
        directory = store.run(submission["run_id"])
        receipt_path = directory / "receipt.json"
        receipt = read_json(receipt_path) if receipt_path.exists() else {"schema": SCHEMA, "accepted": False, "phase": "prepared"}
        if receipt.get("phase") == "removed":
            return receipt
        if receipt.get("accepted"):
            return receipt
        if not receipt.get("job_id") and receipt["phase"] == "submitting":
            matches = parse_ini(self.command("-GetJobsFilterIniAnd", "Name=" + submission["name"], "Plugin=ComfyUI"))
            matches = {i: j for i, j in matches.items() if self.command("-GetJobExtraInfoKeyValue", i, "ComfyNativeRunId").strip() == submission["run_id"]}
            if len(matches) > 1:
                raise RuntimeError("Duplicate Deadline mappings require operator reconciliation")
            if not matches:
                return receipt  # Never blindly resubmit an ambiguous operation.
            receipt["job_id"] = next(iter(matches))
            atomic_json(receipt_path, receipt)
        if not receipt.get("job_id"):
            settings = submission["settings"]
            job = {"Plugin": "ComfyUI", "Name": submission["name"], "BatchName": "ComfyUI " + submission["batch_id"],
                "UserName": submission.get("deadline_user", settings["scope"]), "Pool": settings["pool"], "Group": settings["group"],
                "Priority": settings["priority"],
                "InitialStatus": "Suspended", "Frames": 0, "ChunkSize": 1, "ConcurrentTasks": 1,
                "TaskTimeoutMinutes": settings["task_timeout_minutes"],
                "OverrideJobFailureDetection": True, "FailureDetectionJobErrors": 3,
                "ExtraInfoKeyValue0": "ComfyNativeProtocol=1", "ExtraInfoKeyValue1": "ComfyNativeRunId=" + submission["run_id"],
                "ExtraInfoKeyValue2": "ComfyNativeManifest=" + str(directory / "submission.json")}
            if submission.get("plugin_directory"):
                job["CustomPluginDirectory"] = submission["plugin_directory"]
            if settings["workers"]:
                job["Whitelist"] = settings["workers"]
            plugin = {"NativeQueueProtocol": 1, "NativeRunId": submission["run_id"], "NativeRoot": str(store.root),
                "BatchCount": 1, "ChunkSize": 1}
            for name, values in (("job.info", job), ("plugin.info", plugin)):
                (directory / name).write_text("".join(field(k) + "=" + field(v) + "\n" for k, v in values.items()), encoding="utf-8")
            receipt["phase"] = "submitting"
            atomic_json(receipt_path, receipt)
            output = self.command(str(directory / "job.info"), str(directory / "plugin.info"))
            job_id = DeadlineCommandHelper.get_job_id_from_submission(output)
            if not re.fullmatch(r"[0-9a-f]{24}", job_id):
                raise RuntimeError("Deadline did not confirm a Job ID; the request remains unconfirmed")
            receipt.update(job_id=job_id, task_id="0", phase="suspended")
            atomic_json(receipt_path, receipt)
        job_id = receipt["job_id"]
        state = parse_ini(self.command("-GetJob", job_id, "True")).get(job_id, {}).get("Status")
        if state is None:
            return receipt  # refresh confirms absence before closing the run.
        if state == "Suspended":
            if store.control(submission["run_id"])["cancel_requested"]:
                return receipt
            self.command("-ResumeJob", job_id)
            state = parse_ini(self.command("-GetJob", job_id, "True")).get(job_id, {}).get("Status")
            if state is None:
                return receipt
        if state == "Failed":
            receipt["phase"] = "failed"
            atomic_json(receipt_path, receipt)
            return receipt
        if state not in {"Active", "Completed"}:
            raise RuntimeError("Deadline activation is unconfirmed: " + state)
        receipt.update(accepted=True, phase="accepted", accepted_at=int(time.time() * 1000))
        atomic_json(receipt_path, receipt)
        return receipt

    def refresh(self):
        with self.lock:
            settings = dict(self.settings)
            cached_ids = set(self.jobs)
        if not settings["root"]:
            return
        store = RunStore(settings["root"])
        store.check_root()
        submissions = list(store.records(settings["scope"]))
        mappings = {}
        for submission in submissions:
            directory = store.run(submission["run_id"])
            if not (directory / "receipt.json").exists() or not read_json(directory / "receipt.json").get("accepted"):
                with self.lock:
                    recover = settings["enabled"] and self.settings["enabled"] and not self.configuration_in_progress and all(settings[k] == self.settings[k] for k in ("root", "scope"))
                    if recover:
                        self.active_submissions += 1
                if recover:
                    try:
                        with record_lock(store.key(settings["scope"], submission["submission_key"])):
                            with record_lock(directory):
                                self._ensure_submitted(store, submission)
                    finally:
                        with self.lock:
                            self.active_submissions -= 1
            if (directory / "receipt.json").exists():
                receipt = read_json(directory / "receipt.json")
                if receipt.get("job_id"):
                    mappings[receipt["job_id"]] = submission
        remote = {}
        for offset in range(0, len(mappings), 100):
            remote.update(parse_ini(self.command("-GetJob", ",".join(list(mappings)[offset:offset + 100]), "True")))
        absent = [job_id for job_id in mappings if job_id not in remote]
        for offset in range(0, len(absent), 100):
            output = self.command("-ExecuteScript", str(Path(__file__).parent / "scripts" / "deadline_job_status.py"), *absent[offset:offset + 100])
            lines = [line[len("COMFY_NATIVE_JOBS="):] for line in output.splitlines() if line.startswith("COMFY_NATIVE_JOBS=")]
            if len(lines) != 1:
                raise RuntimeError("Deadline did not confirm the missing Jobs; cached runs are retained")
            confirmed = json.loads(lines[0])
            if set(confirmed) != set(absent[offset:offset + 100]) or any(not isinstance(record, dict) or "Status" not in record for record in confirmed.values()):
                raise ValueError("Invalid Deadline Job confirmation")
            remote.update(confirmed)
        jobs, histories, assets, progress = {}, {}, {}, []
        for submission in submissions:
            run_id = submission["run_id"]
            directory = store.run(run_id)
            receipt = read_json(directory / "receipt.json") if (directory / "receipt.json").exists() else {}
            control = store.control(run_id)
            fields = remote.get(receipt.get("job_id"), {})
            if fields.get("Status") in {"Deleted", "Missing"} and receipt.get("job_id"):
                with record_lock(directory):
                    receipt = read_json(directory / "receipt.json")
                    if receipt.get("phase") != "removed":
                        receipt["phase"] = "removed"
                        atomic_json(directory / "receipt.json", receipt)
                    current = store.control(run_id)
                    if not current.get("result") and not current.get("deadline_removed"):
                        current.update(generation=current["generation"] + 1, deadline_removed=True,
                            status="cancelled" if current.get("cancel_requested") else "failed", finished_at=int(time.time() * 1000))
                        if current["status"] == "failed":
                            current["error"] = {"exception_type": "DeadlineJobRemoved", "exception_message":
                                "The Job was removed from Deadline before a result was committed. Retry to submit a new run."}
                        atomic_json(directory / "control.json", current)
                control = store.control(run_id)
            elif control.get("cancel_requested") and control["status"] not in {"cancelled", "completed"} and receipt.get("job_id") and fields:
                if fields.get("Status") not in {"Suspended", "Failed", "Completed"}:
                    self.command("-SuspendJob", receipt["job_id"])
                task = self.command("-GetJobTask", receipt["job_id"], "0")
                task_fields = dict(line.split("=", 1) for line in task.splitlines() if "=" in line)
                task_stopped = task_fields.get("Status") in {"Pending", "Queued", "Completed", "Failed", "Suspended"}
                if task_stopped and (control["generation"] == 0 or control.get("execution_mode") == "task-owned"):
                    with record_lock(directory):
                        current = store.control(run_id)
                        if current.get("cancel_requested") and not current.get("result") and current["generation"] == control["generation"]:
                            current.update(status="cancelled", finished_at=int(time.time() * 1000))
                            atomic_json(directory / "control.json", current)
                    control = store.control(run_id)
            status = control["status"]
            error = control.get("error")
            result = None
            if control.get("result"):
                cache_key = (settings["root"], run_id, control["result"])
                if cache_key not in self.result_cache:
                    self.result_cache[cache_key] = store.result(run_id)
                result = self.result_cache[cache_key]
                status = "completed"
                if fields.get("Status") in {"Failed", "Active", "Suspended"} and not control.get("cancel_requested"):
                    # Result won before acknowledgement loss. No rerender is needed.
                    self.command("-CompleteJob", receipt["job_id"])
            elif fields.get("Status") in {"Failed", "Completed"} and status != "cancelled":
                status = "failed"
                error = error or {"exception_type": "DeadlineError", "exception_message":
                    "Deadline " + fields["Status"] + " without a committed ComfyUI result. Inspect task reports."}
            elif status == "failed" and fields.get("Status") == "Active":
                status = "pending"  # Deadline infrastructure retries remain one run.
            elif fields.get("Status") == "Suspended" and not control.get("cancel_requested"):
                status = "pending"  # External suspension is diagnostic, never user cancellation.
            outputs = copy.deepcopy(result["outputs"]) if result else {}
            file_assets = []
            if result:
                for item in result["files"]:
                    name = Path(item["path"]).name
                    output = {"filename": name, "subfolder": "deadline-native/" + run_id + "/" + str(Path(item["path"]).parent).replace("\\", "/"), "type": "output"}
                    file_assets.append({**output, "node_id": item["node_id"], "output_key": item["output_key"],
                        "output_index": item["output_index"], "size": item["size"], "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                        "id": str(uuid.uuid5(uuid.UUID(run_id), item["path"])), "name": name, "hash": item["sha256"],
                        "preview_url": "/view?" + urlencode(output)})
                    self._cache_output(store, run_id, item)
                    outputs[item["node_id"]][item["output_key"]][item["output_index"]] = output
            files = [i for i in file_assets if i["mime_type"].startswith(("image/", "video/", "audio/"))]
            job = {"id": run_id, "status": status, "create_time": submission["created_at"],
                "workflow_id": submission["workflow_id"], "outputs_count": len(file_assets), "previewable_outputs_count": len(files),
                "workflow": submission["payload"], "outputs": outputs,
                "execution_start_time": control.get("started_at"), "execution_end_time": control.get("finished_at"),
                "deadline": {"job_id": receipt.get("job_id"), "worker": control.get("worker"), "status": fields.get("Status", "Unconfirmed"),
                    "accepted": receipt.get("accepted", False), "retry_of": submission.get("retry_of"), "cancel_requested": control.get("cancel_requested")}}
            if files:
                first = files[0]
                job["preview_output"] = {k: first[k] for k in ("filename", "subfolder", "type")}
                job["preview_output"].update(nodeId=first["node_id"], mediaType=first["output_key"])
            messages = result.get("execution_status", {}).get("messages", []) if result else []
            if status == "cancelled":
                messages = [["execution_start", {"timestamp": control.get("started_at", submission["created_at"])}],
                    ["execution_interrupted", {"prompt_id": run_id, "timestamp": control.get("finished_at", int(time.time() * 1000))}]]
            if error and status == "failed":
                detail = {"prompt_id": run_id, "node_id": "", "node_type": "Deadline", "traceback": [],
                    "current_inputs": {}, "current_outputs": {}, **error}
                detail["prompt_id"] = run_id
                job["execution_error"] = detail
                messages = [["execution_error", detail]]
            job["execution_status"] = {"completed": status == "completed", "status_str": "success" if status == "completed" else "error", "messages": messages}
            histories[run_id] = {"prompt": [0, run_id, submission["payload"]["prompt"], submission["payload"]["extra_data"], list(submission["payload"]["prompt"])],
                "outputs": outputs, "status": job["execution_status"]}
            assets[run_id] = file_assets
            if not control.get("hidden"):
                jobs[run_id] = job
            event = {"run_id": run_id, "generation": control["generation"], "sequence": 0, "status": status, "nodes": {}}
            attempt = control.get("attempt_id")
            if attempt and (directory / "attempts" / attempt / "progress.json").exists():
                sample = read_json(directory / "attempts" / attempt / "progress.json")
                if sample.get("run_id") == run_id and sample.get("generation") == control["generation"]:
                    event.update(sample)
            # A terminal/control transition must outrank its last live sample;
            # otherwise the browser's sequence fence retains a running preview.
            event["sequence"] = event["sequence"] * 4 + (2 if status in TERMINAL else int(bool(control.get("cancel_requested"))))
            event.update(status=status, cancel_requested=control.get("cancel_requested", False))
            for node in event["nodes"].values():
                node["prompt_id"] = run_id
            if not control.get("hidden"):
                progress.append(event)
        with self.lock:
            if any(self.settings[k] != settings[k] for k in ("root", "scope")):
                return
            for run_id, job in self.jobs.items():
                if run_id not in jobs and run_id not in cached_ids:
                    jobs[run_id] = job
                    histories[run_id] = self.histories[run_id]
                    assets[run_id] = self.assets[run_id]
            self.jobs, self.histories, self.assets, self.progress = jobs, histories, assets, progress
            self.health = {"connected": True, "message": "Deadline and shared records are available", "last_refresh": int(time.time() * 1000)}

    def _cache_output(self, store, run_id, asset):
        # Native Add to workflow writes an annotated [output] path into a
        # LoadImage/Video/Audio widget. Materialize that namespace in the host's
        # normal output root; the share and accepted manifest remain authoritative.
        if self.output_root is None:
            return
        target = safe_path(self.output_root, "deadline-native/" + run_id + "/" + asset["path"])
        if target.is_file() and target.stat().st_size == asset["size"]:
            return
        source = safe_path(store.run(run_id), asset["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copy2(source, temporary)
            if self._hash_file(temporary) != asset["sha256"]:
                raise ValueError("Output cache hash mismatch")
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def list_jobs(self):
        with self.lock:
            return copy.deepcopy(list(self.jobs.values()))

    async def get_job(self, run_id):
        with self.lock:
            return copy.deepcopy(self.jobs.get(run_id))

    async def get_assets(self, run_id):
        with self.lock:
            return copy.deepcopy(self.assets.get(run_id, []))

    async def get_history(self, run_id):
        with self.lock:
            return copy.deepcopy(self.histories[run_id])

    async def view(self, request):
        def resolve():
            parts = request.query.get("subfolder", "").split("/")
            run_id = canonical_id(parts[1])
            with self.lock:
                if run_id not in self.jobs or self.jobs[run_id]["status"] != "completed":
                    raise ValueError("No accepted result in this user scope")
            relative = "/".join(parts[2:]) + "/" + request.query.get("filename", "")
            store = RunStore(self.settings["root"])
            result = store.result(run_id)
            if not result or not any(asset["path"] == relative for asset in result["files"]):
                raise ValueError("Asset is not in the accepted result manifest")
            return safe_path(store.run(run_id), relative)
        try:
            path = await asyncio.to_thread(resolve)
            return web.FileResponse(path)
        except (ValueError, OSError, KeyError, IndexError):
            return web.Response(status=404)

    async def cancel(self, run_id):
        with self.lock:
            if self.jobs.get(run_id, {}).get("status") in TERMINAL:
                return False
        changed = await asyncio.to_thread(RunStore(self.settings["root"]).cancel, run_id)
        if changed:
            with self.lock:
                job = self.jobs.get(run_id)
                if job and job["status"] not in TERMINAL:
                    job["deadline"]["cancel_requested"] = True
                for event in self.progress:
                    if event["run_id"] == run_id and event["status"] not in TERMINAL:
                        event["cancel_requested"] = True
                        event["sequence"] = event["sequence"] // 4 * 4 + 1
        self.wakeup.set()
        return changed

    async def hide(self, run_id):
        def perform():
            store = RunStore(self.settings["root"])
            with record_lock(store.run(run_id)):
                state = store.control(run_id)
                if state["status"] not in TERMINAL:
                    with self.lock:
                        if self.jobs.get(run_id, {}).get("status") not in TERMINAL:
                            raise ValueError("Only terminal history can be hidden")
                state["hidden"] = True
                atomic_json(store.run(run_id) / "control.json", state)
        await asyncio.to_thread(perform)
        self.wakeup.set()

    def retry(self, run_id, key):
        if not self.settings["enabled"]:
            raise ValueError("Native Deadline queue is disabled")
        store = RunStore(self.settings["root"])
        original = read_json(store.run(run_id) / "submission.json")
        if original["scope"] != self.settings["scope"]:
            raise ValueError("Run is outside this user scope")
        with self.lock:
            status = self.jobs.get(run_id, {}).get("status", store.control(run_id)["status"])
        if status not in TERMINAL:
            raise ValueError("Only terminal runs may be retried")
        # Retained staged inputs permit retry when the workstation is gone.
        body = {**copy.deepcopy(original["payload"]), "submission_key": key, "batch_id": original["batch_id"]}
        return self.submit(body, captured=original["settings"], retry_of=run_id)


def register_backend():
    if os.environ.get("DEADLINE_SLAVE_NAME"):
        return None
    try:
        import server
        instance = server.PromptServer.instance
    except (ImportError, AttributeError):
        return None
    if instance is None:
        return None
    backend = FarmBackend()
    try:
        import comfyui_version
        version = comfyui_version.__version__
    except (ImportError, AttributeError):
        version = "unknown"
    supported = version == "0.39.0"
    if supported:
        instance.app.middlewares.append(NativeJobsAdapter(backend).middleware)

    @instance.routes.get("/deadline/farm/capabilities")
    async def capabilities(_request):
        with backend.lock:
            return web.json_response({"protocol": SCHEMA, "ready": backend.readiness_cache["ready"] and supported, "mode": "native" if supported else "unsupported",
                "frontend_version": "1.53.10", "transport": "deadlinecommand",
                "health": backend.health, "message": backend.health["message"] if supported else "Native queue requires ComfyUI 0.39.0; found " + version})

    @instance.routes.get("/deadline/farm/settings")
    async def settings(_request):
        with backend.lock:
            return web.json_response({"settings": backend.settings, "health": backend.health, "readiness": backend.readiness_cache})

    async def action(request, callback):
        try:
            return web.json_response(await asyncio.to_thread(callback, await request.json()))
        except SettingsConflict as exc:
            return web.json_response({"error": {"type": "deadline_settings_conflict", "message": str(exc)}, "node_errors": {}}, status=409)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": {"type": "deadline_validation_error", "message": str(exc)}, "node_errors": {}}, status=400)
        except Exception as exc:
            return web.json_response({"error": {"type": "deadline_unconfirmed", "message": str(exc)}, "node_errors": {}}, status=503)

    @instance.routes.post("/deadline/farm/settings")
    async def configure(request):
        return await action(request, lambda body: backend.configure(body["settings"], body["expected_settings"]) if "expected_settings" in body else backend.configure(body))

    @instance.routes.get("/deadline/farm/discovery")
    async def discovery(request):
        return web.json_response(await asyncio.to_thread(backend.discover, request.query.get("refresh") == "1"))

    @instance.routes.get("/deadline/farm/readiness")
    async def readiness(request):
        return web.json_response(await asyncio.to_thread(backend.readiness, force=request.query.get("refresh") == "1"))

    @instance.routes.get("/deadline/farm/setup-guide")
    async def setup_guide(_request):
        return web.Response(text=(Path(__file__).parent / "docs" / "DEADLINE_NATIVE_QUEUE_ADMIN.md").read_text(encoding="utf-8"), content_type="text/plain")

    @instance.routes.post("/deadline/farm/submit")
    async def submit(request):
        if not supported:
            return web.json_response({"error": "Unsupported ComfyUI version"}, status=409)
        return await action(request, backend.submit)

    @instance.routes.post("/deadline/farm/{run_id}/retry")
    async def retry(request):
        if not supported:
            return web.json_response({"error": "Unsupported ComfyUI version"}, status=409)
        return await action(request, lambda body: backend.retry(canonical_id(request.match_info["run_id"]), body["submission_key"]))

    @instance.routes.get("/deadline/farm/progress")
    async def progress(_request):
        with backend.lock:
            return web.json_response({"runs": backend.progress, "health": backend.health, "readiness": backend.readiness_cache})

    @instance.routes.get("/deadline/farm/{run_id}/preview")
    async def preview(request):
        def resolve():
            run_id = canonical_id(request.match_info["run_id"])
            with backend.lock:
                if run_id not in backend.jobs:
                    raise ValueError("Run is outside this user scope")
            store = RunStore(backend.settings["root"])
            state = store.control(run_id)
            if str(state["generation"]) != request.query.get("generation") or not state.get("attempt_id"):
                raise ValueError("Preview is from an old generation")
            event = read_json(store.run(run_id) / "attempts" / state["attempt_id"] / "progress.json")
            return safe_path(store.run(run_id), event["preview_file"])
        try:
            return web.FileResponse(await asyncio.to_thread(resolve))
        except (ValueError, OSError, KeyError):
            return web.Response(status=404)

    async def close(_app):
        backend.shutdown.set()
        backend.wakeup.set()
        if backend.thread:
            await asyncio.to_thread(backend.thread.join, 35)
    instance.app.on_cleanup.append(close)
    if supported:
        backend.start()
    return backend
