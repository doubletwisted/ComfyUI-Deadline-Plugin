"""Worker-local session lifetime only. Install independently of Deadline tasks.

Owned children are tied to a Windows kill-on-close job object. Borrowed artist
processes are never adopted by that object or terminated. Scheduling is Deadline's.
"""

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from farm_protocol import atomic_json, read_json


class BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
        ("process_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class OwnedProcess:
    def __init__(self, command, cwd, log):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.kernel.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        self.process = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | 4)  # CREATE_SUSPENDED
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(self.process._handle))):
            self.process.kill()
            self.process.wait()
            self.kernel.CloseHandle(self.handle)
            raise ctypes.WinError(ctypes.get_last_error())
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        if ntdll.NtResumeProcess(wintypes.HANDLE(int(self.process._handle))) != 0:
            self.close()
            raise RuntimeError("Cannot resume supervised ComfyUI process")

    def close(self):
        self.kernel.CloseHandle(self.handle)
        self.process.wait(timeout=15)


def comfy_request(url, body=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


class SessionOwner:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.owned = None
        self.log = None
        self.session_id = None
        self.endpoint_identity = None
        self.reservation = None
        self.last_active = time.monotonic()
        self.draining = False
        self.cold_start_seconds = None
        self.url = args.borrowed_url or f"http://127.0.0.1:{args.comfy_port}"
        self.borrowed = bool(args.borrowed_url)
        if self.borrowed:
            address = urlsplit(args.borrowed_url)
            if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost", "::1"} or address.username or address.password or address.path not in {"", "/"} or address.query or address.fragment:
                raise ValueError("Borrowed sessions must use a worker-local HTTP endpoint")
            self.url = args.borrowed_url.rstrip("/")
        self.progress = {}
        self.preview = None
        self.events_stop = threading.Event()
        self.events_ready = threading.Event()
        args.state_root.mkdir(parents=True, exist_ok=True)
        self.input_root = args.state_root / "input"
        self.output_root = args.state_root / "output"
        self.input_root.mkdir(exist_ok=True)
        self.output_root.mkdir(exist_ok=True)
        self.temp_root = args.state_root / "temp" / "temp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        if self.borrowed:
            if not args.borrowed_input_root or not args.borrowed_output_root:
                raise ValueError("Borrowed sessions require their real input and output roots")
            self.input_root, self.output_root = args.borrowed_input_root, args.borrowed_output_root
            self.temp_root = args.borrowed_temp_root or self.temp_root
            path = args.state_root / "reservation.json"
            if path.exists():
                prior = read_json(path)
                if prior.get("reservation"):
                    current = comfy_request(self.url + "/deadline/session")
                    if any(current.get(k) != prior["endpoint_identity"].get(k) for k in ("pid", "session_id", "comfyui_root")):
                        raise ValueError("Borrowed endpoint changed while a reservation was retained; reconcile its old prompt")
                    self.endpoint_identity, self.session_id = current, current["session_id"]
                    self.reservation = prior["reservation"]
                    self.reservation["last_client_at"] = time.monotonic() - 16

    def persist_reservation(self):
        atomic_json(self.args.state_root / "reservation.json", {"endpoint_identity": self.endpoint_identity,
            "reservation": self.reservation})

    def clear_reservation(self):
        token = (self.reservation or {}).get("token")
        self.reservation = None
        self.persist_reservation()
        self.events_stop.set()
        self.preview = None
        if token:
            for suffix in (".png", ".jpg", ".tmp"):
                try:
                    (self.args.state_root / ("preview-" + token + suffix)).unlink(missing_ok=True)
                except OSError:
                    pass  # A preview reader must not prevent GPU handoff.
        self.last_active = time.monotonic()

    def assignment_active(self):
        """Unknown/unavailable is never authority to stop a reservation."""
        command = Path(os.environ.get("DEADLINE_PATH", r"C:\Program Files\Thinkbox\Deadline10\bin")) / "deadlinecommand.exe"
        try:
            checked = subprocess.run([str(command), "-GetJobTask", self.reservation["job_id"], "0"],
                capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            return None
        if checked.returncode or any(line.startswith("Error:") for line in checked.stdout.splitlines()):
            return None
        fields = dict(line.split("=", 1) for line in checked.stdout.splitlines() if "=" in line)
        if "Status" not in fields or "SlaveName" not in fields:
            return None
        return fields["SlaveName"] == self.args.worker and fields["Status"] == "Rendering"

    def identity(self):
        return {"protocol": 1, "mode": "borrowed" if self.borrowed else "owned", "worker": self.args.worker, "gpu_uuid": self.args.gpu_uuid,
            "comfyui_root": str(self.args.comfy_root), "owner_pid": os.getpid(),
            "pid": self.owned.process.pid if self.owned else (self.endpoint_identity or {}).get("pid"), "session_id": self.session_id,
            "endpoint_identity": self.endpoint_identity, "draining": self.draining,
            "reservation": self.reservation, "cold_start_seconds": self.cold_start_seconds,
            "url": self.url, "input_root": str(self.input_root), "output_root": str(self.output_root),
            "temp_root": str(self.temp_root), "exclusive_worker": self.args.exclusive_worker}

    def start(self):
        if self.borrowed:
            identity = comfy_request(self.url + "/deadline/session")
            if Path(identity["comfyui_root"]).resolve() != self.args.comfy_root.resolve():
                raise ValueError("Borrowed endpoint installation does not match")
            gpu_lines = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,gpu_uuid", "--format=csv,noheader"], text=True)
            if not any(line.split(",")[0].strip() == str(identity["pid"]) and line.split(",")[1].strip() == self.args.gpu_uuid for line in gpu_lines.splitlines()):
                raise ValueError("Cannot prove borrowed endpoint PID/GPU UUID; use a configured owned session")
            self.endpoint_identity, self.session_id = identity, identity["session_id"]
            return
        try:
            comfy_request(self.url + "/system_stats")
        except (urllib.error.URLError, TimeoutError):
            pass
        else:
            raise ValueError("Test port is occupied; an existing artist endpoint will not be adopted or killed")
        gpu_lines = subprocess.check_output(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"], text=True)
        indices = {line.split(",")[1].strip(): line.split(",")[0].strip() for line in gpu_lines.splitlines()}
        if self.args.gpu_uuid not in indices:
            raise ValueError("GPU UUID is not present on this Worker")
        python = self.args.comfy_root.parent / "python_embeded" / "python.exe"
        command = [str(python), "-s", str(self.args.comfy_root / "main.py"), "--port", str(self.args.comfy_port),
            "--cuda-device", indices[self.args.gpu_uuid], "--disable-auto-launch",
            "--preview-method", getattr(self.args, "preview_method", "auto"),
            "--input-directory", str(self.input_root), "--output-directory", str(self.output_root),
            "--temp-directory", str(self.temp_root.parent)]
        if self.args.custom_nodes:
            command += ["--disable-all-custom-nodes", "--whitelist-custom-nodes", *self.args.custom_nodes]
        command += self.args.extra_args
        self.log = (self.args.state_root / "comfy.log").open("ab", buffering=0)
        self.owned = OwnedProcess(command, str(self.args.comfy_root), self.log)
        started = time.monotonic()
        deadline = started + getattr(self.args, "startup_seconds", 120)
        while time.monotonic() < deadline:
            if self.owned.process.poll() is not None:
                raise RuntimeError("Owned ComfyUI exited at startup; inspect comfy.log")
            try:
                identity = comfy_request(self.url + "/deadline/session")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise RuntimeError("Worker ComfyUI installation lacks ComfyUI-Deadline-Plugin (/deadline/session returned 404)") from exc
                raise
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.25)
                continue
            if identity["pid"] != self.owned.process.pid or Path(identity["comfyui_root"]).resolve() != self.args.comfy_root.resolve():
                raise RuntimeError("Owned endpoint PID/root did not match the launched process")
            self.endpoint_identity = identity
            self.session_id = str(uuid.uuid4())
            self.cold_start_seconds = time.monotonic() - started
            return
        raise TimeoutError("Owned ComfyUI startup timed out")

    def reserve(self, body):
        with self.lock:
            if body.get("worker") != self.args.worker or body.get("gpu_uuid") != self.args.gpu_uuid:
                raise ValueError("Reservation Worker/GPU identity mismatch")
            if self.draining or self.reservation:
                raise ValueError("Session is draining or already reserved")
            if self.owned and self.owned.process.poll() is not None:
                self.stop()
            if self.owned is None and (not self.borrowed or self.endpoint_identity is None):
                try:
                    self.start()
                except Exception:
                    self.stop()
                    raise
            self.reservation = {"token": str(uuid.uuid4()), "run_id": str(uuid.UUID(body["run_id"])),
                "attempt_id": str(uuid.UUID(body["attempt_id"])), "job_id": body.get("job_id"),
                "prompt_id": None, "reserved_at": time.time(), "last_client_at": time.monotonic(),
                "client_id": str(uuid.uuid4())}
            self.persist_reservation()
            self.progress, self.preview = {}, None
            self.events_stop = threading.Event()
            self.events_ready = threading.Event()
            threading.Thread(target=self.read_events, args=(self.reservation["token"], self.reservation["client_id"], self.events_stop), daemon=True).start()
            self.events_ready.wait(2)
            self.last_active = time.monotonic()
            return self.identity()

    def queue(self, body):
        with self.lock:
            self.verify_token(body)
            if self.reservation["prompt_id"]:
                return {"prompt_id": self.reservation["prompt_id"]}
            self.check_identity()
            self.reservation["prompt_id"] = str(uuid.uuid4())
            self.persist_reservation()  # Recover exact prompt after a lost response/owner death.
            payload = dict(body["payload"], client_id=self.reservation["client_id"], prompt_id=self.reservation["prompt_id"])
            result = comfy_request(self.url + "/prompt", payload)
            if result["prompt_id"] != self.reservation["prompt_id"]:
                raise ValueError("Worker did not preserve the reserved prompt UUID")
            return result

    def verify_token(self, body):
        if self.reservation is None or body.get("token") != self.reservation["token"]:
            raise ValueError("Reservation token does not own this session")
        self.reservation["last_client_at"] = time.monotonic()

    def check_identity(self):
        identity = comfy_request(self.url + "/deadline/session")
        if not self.endpoint_identity or any(identity.get(k) != self.endpoint_identity.get(k) for k in ("pid", "session_id", "comfyui_root")):
            raise ValueError("Worker endpoint identity changed; prompt was not dispatched")

    def read_events(self, token, client_id, stop):
        import asyncio
        import aiohttp
        async def read():
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(self.url.replace("http", "ws", 1) + "/ws?clientId=" + client_id, heartbeat=15) as socket:
                    self.events_ready.set()
                    while not stop.is_set():
                        try:
                            message = await socket.receive(timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        with self.lock:
                            if self.reservation is None or self.reservation["token"] != token:
                                return
                            if message.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(message.data)
                                data = event.get("data", {})
                                if data.get("prompt_id") != self.reservation["prompt_id"]:
                                    continue
                                if event["type"] == "progress" and data.get("node"):
                                    node = str(data["node"])
                                    self.progress[node] = {"node_id": node, "prompt_id": self.reservation["run_id"],
                                        "value": data["value"], "max": data["max"], "state": "running"}
                                elif event["type"] == "progress_state":
                                    self.progress = data.get("nodes", {})
                                elif event["type"] == "executing" and data.get("node"):
                                    node = str(data["node"])
                                    self.progress[node] = {"node_id": node, "prompt_id": self.reservation["run_id"], "value": 0, "max": 1, "state": "running"}
                            elif message.type == aiohttp.WSMsgType.BINARY and 8 < len(message.data) <= 4 * 1024 ** 2:
                                # Legacy previews arrive only on this reservation's
                                # client socket. They never touch the browser canvas.
                                raw = message.data
                                image_bytes = None
                                if int.from_bytes(raw[:4], "big") == 1:
                                    extension = ".jpg" if int.from_bytes(raw[4:8], "big") == 1 else ".png"
                                    image_bytes = raw[8:]
                                elif int.from_bytes(raw[:4], "big") == 4:
                                    length = int.from_bytes(raw[4:8], "big")
                                    if 0 < length < len(raw) - 8:
                                        metadata = json.loads(raw[8:8 + length])
                                        if metadata.get("prompt_id") == self.reservation["prompt_id"]:
                                            extension = ".png" if metadata.get("image_type") == "image/png" else ".jpg"
                                            image_bytes = raw[8 + length:]
                                if image_bytes:
                                    path = self.args.state_root / ("preview-" + token + extension)
                                    temporary = path.with_suffix(".tmp")
                                    temporary.write_bytes(image_bytes)
                                    os.replace(temporary, path)
                                    self.preview = str(path)
        try:
            asyncio.run(read())
        except Exception as exc:
            print("Preview/progress connection unavailable: " + str(exc), flush=True)
        finally:
            self.events_ready.set()

    def status(self, body):
        with self.lock:
            self.verify_token(body)
            self.check_identity()
            prompt_id = self.reservation["prompt_id"]
            if prompt_id is None:
                return {"state": "reserved"}
            history = comfy_request(self.url + "/history/" + prompt_id)
            if prompt_id in history:
                return {"state": "finished", "history": history[prompt_id], "identity": self.identity(), "nodes": self.progress, "preview": self.preview}
            queue = comfy_request(self.url + "/queue?deadline_local=1")
            state = "running" if any(item[1] == prompt_id for item in queue["queue_running"]) else (
                "pending" if any(item[1] == prompt_id for item in queue["queue_pending"]) else "missing")
            return {"state": state, "identity": self.identity(), "nodes": self.progress, "preview": self.preview}

    def cancel(self, body):
        with self.lock:
            self.verify_token(body)
            if self.reservation["prompt_id"]:
                self.check_identity()
                return comfy_request(self.url + "/api/jobs/" + self.reservation["prompt_id"] + "/cancel", {})
            self.clear_reservation()
            if not self.borrowed and not self.args.exclusive_worker:
                self.stop()  # Release GPU before Deadline releases its task slot.
            return {"cancelled": True}

    def release(self, body):
        with self.lock:
            result = self.status(body)
            if result["state"] not in {"finished", "missing"}:
                raise ValueError("Cannot release a reservation while its prompt is active")
            self.clear_reservation()
            if not self.borrowed and not self.args.exclusive_worker:
                self.stop()
            return {"released": True}

    def stop(self):
        self.events_stop.set()
        if self.owned:
            self.owned.close()
            self.owned = None
        if self.log:
            self.log.close()
            self.log = None
        self.session_id = self.endpoint_identity = None

    def idle_tick(self):
        with self.lock:
            if self.owned and self.owned.process.poll() is not None:
                self.stop()
                self.clear_reservation()
            if not self.owned and not self.borrowed:
                return
            if self.reservation:
                # A vanished waiter/heartbeat is never permission to kill a
                # rendering prompt. Reconcile only the exact recorded prompt.
                token = self.reservation["token"]
                if self.reservation["prompt_id"] is None and time.time() - self.reservation["reserved_at"] > 120 and self.assignment_active() is False:
                    self.clear_reservation()
                    if not self.borrowed and not self.args.exclusive_worker:
                        self.stop()
                    return
                if self.reservation["prompt_id"]:
                    # Preserve the client-heartbeat timestamp while observing.
                    last_client = self.reservation["last_client_at"]
                    result = self.status({"token": token})
                    self.reservation["last_client_at"] = last_client
                    if time.monotonic() - last_client > 15:
                        # Task/sandbox death does not implicitly kill a session.
                        # Verify the authoritative assignment before interrupting
                        # only this reservation's prompt. Heartbeat age alone
                        # cannot authorize terminating active work.
                        self.reservation.setdefault("last_assignment_check", 0)
                        if time.monotonic() - self.reservation["last_assignment_check"] > 5:
                            self.reservation["last_assignment_check"] = time.monotonic()
                            if self.assignment_active() is False:
                                if result["state"] not in {"finished", "missing"}:
                                    self.check_identity()
                                    comfy_request(self.url + "/api/jobs/" + self.reservation["prompt_id"] + "/cancel", {})
                                else:
                                    self.clear_reservation()
                                    if not self.borrowed and not self.args.exclusive_worker:
                                        self.stop()
                return
            if time.monotonic() - self.last_active < self.args.idle_seconds:
                return
            self.draining = True
            queue = comfy_request(self.url + "/queue?deadline_local=1")
            if queue["queue_running"] or queue["queue_pending"]:
                self.draining = False
                return
            if not self.borrowed:
                self.stop()
            self.draining = False


def serve(args):
    os.environ["DEADLINE_SLAVE_NAME"] = args.worker
    os.environ["GIT_PYTHON_REFRESH"] = "quiet"
    owner = SessionOwner(args)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            with owner.lock:
                self.respond(200, owner.identity())

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 16 * 1024 ** 2:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                actions = {"/reserve": owner.reserve, "/queue": owner.queue, "/status": owner.status,
                    "/cancel": owner.cancel, "/release": owner.release}
                if self.path not in actions:
                    self.respond(404, {"error": "Unknown session action"})
                    return
                self.respond(200, actions[self.path](body))
            except Exception as exc:
                self.respond(409, {"error": str(exc)})

        def respond(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", args.broker_port), Handler)
    server.timeout = 1
    try:
        while True:
            server.handle_request()
            try:
                owner.idle_tick()
            except (urllib.error.URLError, TimeoutError) as exc:
                print(f"Session endpoint unavailable; retaining ownership: {exc}", flush=True)
    finally:
        with owner.lock:
            owner.stop()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--comfy-port", type=int, default=8198)
    parser.add_argument("--broker-port", type=int, default=8201)
    parser.add_argument("--idle-seconds", type=float, default=60)
    parser.add_argument("--startup-seconds", type=float, default=120)
    parser.add_argument("--preview-method", choices=("none", "auto", "latent2rgb", "taesd"), default="auto")
    parser.add_argument("--exclusive-worker", action="store_true", help="Keep VRAM warm only on Workers reserved for ComfyUI")
    parser.add_argument("--borrowed-url", default="")
    parser.add_argument("--borrowed-input-root", type=Path)
    parser.add_argument("--borrowed-output-root", type=Path)
    parser.add_argument("--borrowed-temp-root", type=Path)
    parser.add_argument("--custom-nodes", nargs="*", default=[])
    parser.add_argument("--extra-args", nargs="*", default=[])
    serve(parser.parse_args())
