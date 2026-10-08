"""Explicit native-queue mode for the existing ComfyUI Deadline plugin.

The sandbox prepares with the existing preflight. A small supervised client
tracks only its reservation/prompt and commits validated files before success.
"""
import argparse
import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

# Windows portable Python uses an isolated ._pth and omits the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from farm_protocol import RunStore, SCHEMA, atomic_json, canonical_id, digest, read_json, safe_path


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_task_owner(plugin, startup_seconds=120):
    """A cold render owns its supervisor and GPU child for this task only."""
    from farm_session_owner import OwnedProcess
    plugin._set_deadline_environment_variables()
    portable = Path(plugin._resolve_comfyui_install_path())
    gpu = plugin._get_gpu_from_worker_affinity()
    if gpu is None:
        gpu = plugin._get_default_cuda_device()
    if gpu is None:
        raise ValueError("Configure a Deadline GPU affinity or default CUDA device")
    output = subprocess.check_output(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], text=True, timeout=10)
    devices = dict([value.strip() for value in line.split(",", 1)] for line in output.splitlines() if line.strip())
    if str(gpu) not in devices:
        raise ValueError("Deadline assigned a GPU which is not present on this Worker")
    broker_port, comfy_port = free_port(), free_port()
    while comfy_port == broker_port:
        comfy_port = free_port()
    state = Path(plugin.temp_dir) / "session"
    state.mkdir(parents=True, exist_ok=True)
    plugin.native_owner_log = (state / "owner.log").open("ab", buffering=0)
    command = [str(portable / "python_embeded" / "python.exe"), "-s",
        str(Path(__file__).with_name("farm_session_owner.py")),
        "--worker", plugin.GetSlaveName(), "--gpu-uuid", devices[str(gpu)],
        "--comfy-root", str(portable / "ComfyUI"), "--state-root", str(state),
        "--broker-port", str(broker_port), "--comfy-port", str(comfy_port),
        "--startup-seconds", str(startup_seconds)]
    plugin.native_owner = OwnedProcess(command, str(portable), plugin.native_owner_log)
    broker = "http://127.0.0.1:" + str(broker_port)
    until = time.monotonic() + 15
    while time.monotonic() < until:
        if plugin.native_owner.process.poll() is not None:
            raise RuntimeError("Task session owner exited; inspect " + str(state / "owner.log"))
        try:
            identity = request(broker)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(.1)
            continue
        if identity.get("owner_pid") != plugin.native_owner.process.pid:
            raise RuntimeError("Task session owner port belongs to another process")
        return {"broker": broker, "gpu_uuid": devices[str(gpu)], "startup_seconds":startup_seconds}
    raise TimeoutError("Task session owner did not start")


def request(url, body=None, timeout=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"}), timeout=timeout or (150 if url.endswith("/reserve") else 10)) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode(errors="replace")) from exc


def assignment(plugin):
    # Deadline's authoritative assigned task and start time fence old waiters.
    from Deadline.Scripting import RepositoryUtils
    tasks = RepositoryUtils.GetJobTasks(plugin.GetJob(), True)
    task = next(t for t in tasks.Tasks if str(t.TaskId) == "0")
    if str(task.TaskSlaveName) != plugin.GetSlaveName() or str(task.TaskStatus).lower() != "rendering":
        raise ValueError("Deadline task is not currently assigned to this Worker")
    start = task.TaskStartTime.ToUniversalTime().Ticks
    from System.Globalization import CultureInfo
    return {"worker": plugin.GetSlaveName(), "start": int(start),
        "display_start": task.TaskStartTime.ToString("MMM dd/yy  HH:mm:ss", CultureInfo.InvariantCulture)}


def prepare(plugin):
    if getattr(plugin, "native_owner", None):
        cleanup(plugin)
    run_id = canonical_id(plugin.GetPluginInfoEntry("NativeRunId"))
    root = plugin.GetPluginInfoEntry("NativeRoot")
    if not root.startswith("\\\\"):
        raise ValueError("Native payload storage must be an off-host UNC root")
    store = RunStore(root)
    store.check_root()
    submission = read_json(store.run(run_id) / "submission.json")
    if submission["schema"] != SCHEMA or digest(submission["payload"]) != submission["snapshot_hash"]:
        raise ValueError("Unsupported or modified native snapshot")
    mapping = json.loads(plugin.GetConfigEntryWithDefault("NativeWorkerSessions", "{}"))
    config = next((v for k, v in mapping.items() if k.lower() == plugin.GetSlaveName().lower()), None)
    if config is None:
        from Deadline.Scripting import RepositoryUtils
        settings = RepositoryUtils.GetSlaveSettings(plugin.GetSlaveName(), True)
        registration = settings.GetSlaveExtraInfoKeyValueWithDefault("ComfyNativeSession", "")
        config = json.loads(registration) if registration else None
    attempt_id = str(uuid.uuid4())
    job_id = str(plugin.GetJob().JobId)
    state = store.begin_attempt(run_id, attempt_id, job_id, plugin.GetSlaveName(), assignment(plugin),
        execution_mode="task-owned" if config is None else "persistent")
    spec = {"root": root, "run_id": run_id, "attempt_id": attempt_id, "job_id": job_id, "skip": state is None}
    plugin.native_spec = spec
    plugin.temp_dir = plugin.CreateTempDirectory("comfyui_native_task")
    if state is not None:
        spec["generation"] = state["generation"]
        spec["assignment"] = state["assignment"]
        try:
            if config is None:
                config = start_task_owner(plugin, submission["settings"]["task_timeout_minutes"] * 60)
            if not isinstance(config, dict):
                raise ValueError("Invalid NativeWorkerSessions entry for " + plugin.GetSlaveName())
            broker = config.get("broker", "http://127.0.0.1:8202").rstrip("/")
            url = urllib.parse.urlparse(broker)
            if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost"} or url.username or url.path:
                raise ValueError("Session broker must be a worker-local loopback HTTP endpoint")
            spec["broker"] = broker
            identity = request(broker + "/reserve", {"worker": plugin.GetSlaveName(), "gpu_uuid": config["gpu_uuid"],
                "run_id": run_id, "attempt_id": attempt_id, "job_id": job_id}, timeout=config.get("startup_seconds",120) + 30)
            spec.update(token=identity["reservation"]["token"], identity=identity)
            plugin.native_spec = spec  # Cleanup can cancel even if preflight raises.
            if config.get("comfyui_root"):
                configured_root = Path(config["comfyui_root"])
                if not (configured_root / "main.py").is_file() or not (configured_root.parent / "python_embeded" / "python.exe").is_file():
                    raise ValueError("The native session mapping's portable ComfyUI installation is missing")
                plugin.comfyui_install_path = str(configured_root.parent)
            portable = Path(plugin._resolve_comfyui_install_path())
            if Path(identity["comfyui_root"]).resolve() != (portable / "ComfyUI").resolve():
                raise ValueError("Session owner installation differs from Deadline ComfyUIPath")
            plugin.configured_launch_gpu_uuid = config["gpu_uuid"]
            plugin._get_policy_launch_gpu()  # UUID present locally and inside Deadline GPU affinity.
            payload = copy.deepcopy(submission["payload"])
            stage_inputs(store, run_id, attempt_id, payload, identity)
            namespace = "deadline-native/" + run_id + "/" + attempt_id
            for node in payload["prompt"].values():
                for name in ("filename_prefix", "output_filename_prefix"):
                    if name in node["inputs"]:
                        original = node["inputs"][name]
                        if not isinstance(original, str):
                            raise ValueError("Dynamic output filename prefixes are unsupported in native queue mode")
                        node["inputs"][name] = namespace + "/" + Path(original.replace("\\", "/")).name
            plugin.use_existing_comfyui = True
            plugin.reuse_gui_input_root = identity["input_root"]
            plugin.object_info = request(identity["url"] + "/object_info")
            workflow = payload["extra_data"]["extra_pnginfo"]["workflow"]
            payload["prompt"], expected = plugin._preflight_worker_payload(payload["prompt"], workflow)
            spec.update(payload=payload, expected_outputs=expected, timeout_seconds=submission["settings"]["task_timeout_minutes"] * 60)
            stats = request(identity["url"] + "/system_stats")
            store.update_attempt(run_id, attempt_id, state["generation"], session_id=identity["session_id"],
                session_mode=identity["mode"], worker_pid=identity["pid"], environment=stats.get("system", {}))
        except Exception as exc:
            log = Path(plugin.temp_dir) / "session" / "comfy.log"
            if log.is_file():
                plugin.LogWarning("ComfyUI startup/preflight log:\n" + "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]))
            store.update_attempt(run_id, attempt_id, state["generation"], status="failed",
                error={"exception_type": type(exc).__name__, "exception_message": str(exc)})
            cleanup(plugin)
            raise
    plugin.native_spec = spec
    path = Path(plugin.temp_dir) / "task.json"
    atomic_json(path, spec)  # Reservation token is worker-local, never in shared records.
    plugin.native_spec_path = str(path)
    return spec


def stage_inputs(store, run_id, attempt_id, payload, identity):
    manifest = read_json(store.run(run_id) / "input_manifest.json")
    mapping = {}
    for asset in manifest["assets"]:
        source = safe_path(store.run(run_id) / "inputs", asset["staged_relative_path"])
        with source.open("rb") as handle:
            checksum = hashlib.sha256(handle.read()).hexdigest()
        if checksum != asset["sha256"]:
            raise ValueError("Staged input changed: " + asset["staged_relative_path"])
        relative = "deadline-native/" + run_id + "/" + attempt_id + "/" + asset["staged_relative_path"]
        target = safe_path(identity["input_root"], relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        mapping[asset["staged_relative_path"]] = relative
        mapping[asset["destination"]] = str(target)
    for node in payload["prompt"].values():
        for name, value in node["inputs"].items():
            if isinstance(value, str) and value in mapping:
                node["inputs"][name] = mapping[value]


def cleanup_reservation(plugin):
    spec = getattr(plugin, "native_spec", None)
    if not spec or not spec.get("token"):
        return
    try:
        status = request(spec["broker"] + "/status", {"token": spec["token"]})
        if status["state"] in {"finished", "missing"}:
            state = RunStore(spec["root"]).control(spec["run_id"])
            if state.get("cancel_requested"):
                RunStore(spec["root"]).update_attempt(spec["run_id"], spec["attempt_id"], spec["generation"],
                    status="cancelled", finished_at=int(time.time() * 1000))
            request(spec["broker"] + "/release", {"token": spec["token"]})
        else:
            request(spec["broker"] + "/cancel", {"token": spec["token"]})
            for _ in range(20):
                status = request(spec["broker"] + "/status", {"token": spec["token"]})
                if status["state"] in {"finished", "missing"}:
                    state = RunStore(spec["root"]).control(spec["run_id"])
                    if state.get("cancel_requested"):
                        RunStore(spec["root"]).update_attempt(spec["run_id"], spec["attempt_id"], spec["generation"],
                            status="cancelled", finished_at=int(time.time() * 1000))
                    request(spec["broker"] + "/release", {"token": spec["token"]})
                    break
                time.sleep(.5)
    except Exception as exc:
        if "Reservation token does not own this session" in str(exc):
            return
        plugin.LogWarning("Native reservation cleanup: " + str(exc))


def cleanup(plugin):
    try:
        cleanup_reservation(plugin)
    finally:
        owner = getattr(plugin, "native_owner", None)
        if owner is not None:
            plugin.native_owner = None
            owner.close()
        log = getattr(plugin, "native_owner_log", None)
        if log is not None:
            plugin.native_owner_log = None
            log.close()


def publish_outputs(store, spec, history):
    outputs = copy.deepcopy(history["outputs"])
    files, seen_nodes = [], set()
    for node_id, groups in outputs.items():
        for group, items in groups.items():
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                kind = item.get("type", "output")
                if kind not in {"output", "temp"}:
                    raise ValueError("Unsupported output file type: " + kind)
                source = safe_path(spec["identity"][kind + "_root"], item.get("subfolder", "") + "/" + item["filename"] if item.get("subfolder") else item["filename"])
                if not source.is_file() or source.stat().st_size <= 0:
                    raise ValueError("Missing ComfyUI output: " + str(source))
                relative = "attempts/" + spec["attempt_id"] + "/outputs/" + str(node_id) + "/" + group + "/" + str(index) + "-" + Path(item["filename"]).name
                destination = safe_path(store.run(spec["run_id"]), relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                with destination.open("rb") as handle:
                    checksum = hashlib.sha256(handle.read()).hexdigest()
                files.append({"path": relative, "size": destination.stat().st_size, "sha256": checksum,
                    "node_id": str(node_id), "output_key": group, "output_index": index})
                seen_nodes.add(str(node_id))
    missing = set(spec["expected_outputs"]) - seen_nodes
    if missing:
        raise ValueError("Output nodes produced no validated files: " + ", ".join(sorted(missing)))
    return {"schema": SCHEMA, "run_id": spec["run_id"], "attempt_id": spec["attempt_id"],
        "generation": spec["generation"], "files": files, "outputs": outputs,
        "execution_status": history["status"], "session_id": spec["identity"]["session_id"]}


def run(spec):
    store = RunStore(spec["root"])
    run_id = spec["run_id"]
    if spec["skip"]:
        if store.control(run_id).get("cancel_requested"):
            return
        if not store.result(run_id):
            raise RuntimeError("Skipped assignment without a valid accepted result")
        print("Progress: 100%", flush=True)
        return
    attempt, generation, token = spec["attempt_id"], spec["generation"], spec["token"]
    broker = spec["broker"]
    try:
        control = store.control(run_id)
        if control.get("cancel_requested") or not store.owns(control, attempt, generation):
            request(broker + "/cancel", {"token": token})
            return
        queued = request(broker + "/queue", {"token": token, "payload": spec["payload"]})
        store.update_attempt(run_id, attempt, generation, worker_prompt_id=queued["prompt_id"])
        until, sequence = time.monotonic() + spec["timeout_seconds"], 0
        while time.monotonic() < until:
            control = store.control(run_id)
            if not store.owns(control, attempt, generation):
                request(broker + "/cancel", {"token": token})
                raise RuntimeError("Attempt was superseded")
            status = request(broker + "/status", {"token": token})
            if status["state"] == "missing":
                raise RuntimeError("The reserved Worker prompt is absent from queue and history")
            if control.get("cancel_requested") and status["state"] != "finished":
                request(broker + "/cancel", {"token": token})
            sequence += 1
            event = {"run_id": run_id, "generation": generation, "sequence": sequence,
                "status": "in_progress", "nodes": status.get("nodes", {})}
            if status.get("preview"):
                source = Path(status["preview"])
                relative = "attempts/" + attempt + "/preview" + source.suffix
                destination = safe_path(store.run(run_id), relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(".tmp")
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
                event["preview_url"] = "/deadline/farm/" + run_id + "/preview?generation=" + str(generation) + "&sequence=" + str(sequence)
                event["preview_file"] = relative
            atomic_json(store.run(run_id) / "attempts" / attempt / "progress.json", event)
            nodes = event["nodes"]
            if nodes:
                fraction = sum(min(n.get("value", 0) / max(n.get("max", 1), 1), 1) for n in nodes.values()) / len(nodes)
                print("Progress: " + str(round(fraction * 99, 1)) + "%", flush=True)
            if status["state"] == "finished":
                if control.get("cancel_requested"):
                    store.update_attempt(run_id, attempt, generation, status="cancelled", finished_at=int(time.time() * 1000))
                    request(broker + "/release", {"token": token})
                    return
                history = status["history"]
                if history["status"]["status_str"] != "success":
                    errors = [m[1] for m in history["status"]["messages"] if m[0] == "execution_error"]
                    error = errors[-1] if errors else {"exception_type": "ComfyUIError", "exception_message": str(history["status"])}
                    store.update_attempt(run_id, attempt, generation, status="failed", error=error)
                    raise RuntimeError(error.get("exception_message", "ComfyUI prompt failed"))
                manifest = publish_outputs(store, spec, history)
                if not store.commit(run_id, attempt, generation, manifest, lambda: assignment_is_current(spec)):
                    if store.control(run_id).get("cancel_requested"):
                        store.update_attempt(run_id, attempt, generation, status="cancelled", finished_at=int(time.time() * 1000))
                    else:
                        raise RuntimeError("Result publication was fenced out")
                request(broker + "/release", {"token": token})
                print("Progress: 100%", flush=True)
                return
            time.sleep(1)
        request(broker + "/cancel", {"token": token})
        raise TimeoutError("Native task timeout; its own prompt was cancelled")
    except Exception as exc:
        store.update_attempt(run_id, attempt, generation, status="failed",
            error={"exception_type": type(exc).__name__, "exception_message": str(exc)})
        try:
            request(broker + "/cancel", {"token": token})
        except Exception:
            pass
        raise


def assignment_is_current(spec):
    command = Path(os.environ.get("DEADLINE_PATH", r"C:\Program Files\Thinkbox\Deadline10\bin")) / "deadlinecommand.exe"
    helper = Path(__file__).with_name("native_queue_assignment.py")
    checked = subprocess.run([str(command), "-ExecuteScript", str(helper), spec["job_id"]], capture_output=True,
        text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if checked.returncode or any(line.startswith("Error:") for line in checked.stdout.splitlines()):
        raise RuntimeError("Could not verify the Deadline assignment before result publication")
    records = [json.loads(line.split("=", 1)[1]) for line in checked.stdout.splitlines() if line.startswith("COMFY_NATIVE_ASSIGNMENT=")]
    if len(records) != 1:
        raise RuntimeError("Deadline did not return an exact task assignment")
    fields = records[0]
    assignment = spec["assignment"]
    return fields.get("status", "").lower() == "rendering" and fields.get("worker") == assignment["worker"] and fields.get("start") == assignment["start"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    run(read_json(parser.parse_args().spec))
