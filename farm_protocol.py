"""Version 1 farm records. Shared verbatim by ComfyUI and the Deadline sandbox.

Windows byte-range locks and same-directory os.replace were exercised on SMB
from M28 and SIM01. No registry depends on the submitting process remaining up.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD]
    _kernel.VirtualAlloc.restype = ctypes.c_void_p
    _kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD]

SCHEMA = 1
TERMINAL = {"completed", "failed", "cancelled"}


def canonical_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected a canonical lowercase UUID")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def safe_path(root, relative):
    root = Path(root).resolve()
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ":" in relative:
        raise ValueError("Expected a relative asset path")
    path = (root / relative).resolve()
    if os.path.commonpath([str(root), str(path)]) != str(root) or path == root:
        raise ValueError("Asset path escapes its root")
    return path


def _read_unc(path):
    # The NAS can retain a read handle to an atomically replaced file. Open
    # briefly without sharing, with DELETE access and no data caching, so a
    # replacement breaks the old handle lease. No delete is performed.
    native = _kernel.CreateFileW(str(path), 0x80010000, 0, None, 3, 0x20000080, None)
    if native == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    # Unbuffered reads require aligned addresses and lengths. VirtualAlloc
    # supplies a page-aligned 64 KiB buffer; EOF may return a shorter record.
    buffer = _kernel.VirtualAlloc(None, 65536, 0x3000, 4)
    if not buffer:
        _kernel.CloseHandle(native)
        raise ctypes.WinError(ctypes.get_last_error())
    chunks = []
    try:
        while True:
            count = wintypes.DWORD()
            if not _kernel.ReadFile(native, buffer, 65536, ctypes.byref(count), None):
                error = ctypes.get_last_error()
                if error == 38:  # ERROR_HANDLE_EOF
                    break
                raise ctypes.WinError(error)
            chunks.append(ctypes.string_at(buffer, count.value))
            if count.value < 65536:
                break
        return b"".join(chunks).decode("utf-8-sig")
    finally:
        _kernel.CloseHandle(native)
        _kernel.VirtualFree(buffer, 0, 0x8000)


def read_json(path):
    path = Path(path)
    deadline = time.monotonic() + 2
    while True:
        try:
            if os.name == "nt" and str(path).startswith("\\\\"):
                return json.loads(_read_unc(path))
            with path.open(encoding="utf-8-sig") as handle:
                return json.load(handle)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.01)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="publish-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.01)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def record_lock(directory, timeout=30):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "control.lock").open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Could not acquire farm record lock: " + str(directory)) from None
                time.sleep(.025)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RunStore:
    def __init__(self, root):
        self.root = Path(root)

    def initialize(self):
        with record_lock(self.root):
            path = self.root / "store.json"
            if not path.exists():
                atomic_json(path, {"schema": SCHEMA, "store_id": str(uuid.uuid4())})
            self.check_root()

    def check_root(self):
        try:
            identity = read_json(self.root / "store.json")
        except FileNotFoundError:
            raise RuntimeError("Farm storage identity is unavailable; reconnect the configured share before submitting") from None
        if identity.get("schema") != SCHEMA:
            raise ValueError("Unsupported farm storage schema")
        canonical_id(identity["store_id"])
        return identity

    def run(self, run_id):
        return self.root / "runs" / canonical_id(run_id)

    def key(self, scope, submission_key):
        return self.root / "requests" / digest(scope)[:24] / canonical_id(submission_key)

    def records(self, scope):
        for path in (self.root / "runs").glob("*/submission.json"):
            submission = read_json(path)
            if submission.get("schema") != SCHEMA:
                raise ValueError("Unsupported farm record schema: " + str(path))
            if submission.get("scope") == scope:
                yield submission

    def control(self, run_id):
        return read_json(self.run(run_id) / "control.json")

    def begin_attempt(self, run_id, attempt_id, job_id, worker, assignment, execution_mode=None):
        directory = self.run(run_id)
        canonical_id(attempt_id)
        with record_lock(directory):
            receipt = read_json(directory / "receipt.json")
            if receipt["job_id"] != job_id:
                raise ValueError("Deadline assignment does not belong to this run")
            state = self.control(run_id)
            if state.get("result") or state.get("cancel_requested") or state.get("deadline_removed"):
                return None
            # Assignment is a Worker plus Deadline task-start timestamp. An old
            # process cannot reclaim a generation after a newer assignment.
            if state.get("assignment") == assignment:
                if state.get("attempt_id") != attempt_id:
                    raise ValueError("This Deadline task assignment already has an attempt")
                return state
            prior_start = state.get("assignment_start", 0)
            if assignment["start"] <= prior_start:
                raise ValueError("Stale Deadline task assignment")
            state.update(generation=state["generation"] + 1, attempt_id=attempt_id,
                worker=worker, assignment=assignment, assignment_start=assignment["start"],
                status="in_progress", started_at=int(time.time() * 1000), error=None)
            state.pop("execution_mode", None)
            if execution_mode is not None:
                state["execution_mode"] = execution_mode
            atomic_json(directory / "control.json", state)
            return state

    def owns(self, state, attempt_id, generation):
        return state.get("attempt_id") == attempt_id and state["generation"] == generation

    def update_attempt(self, run_id, attempt_id, generation, **values):
        directory = self.run(run_id)
        with record_lock(directory):
            state = self.control(run_id)
            if not self.owns(state, attempt_id, generation) or state.get("result") or state.get("status") == "cancelled":
                return False
            state.update(values)
            atomic_json(directory / "control.json", state)
            return True

    def cancel(self, run_id):
        directory = self.run(run_id)
        with record_lock(directory):
            state = self.control(run_id)
            if state.get("result") or state.get("status") in {"completed", "cancelled"} or state.get("deadline_removed"):
                return False
            state.update(cancel_requested=True, cancel_requested_at=int(time.time() * 1000))
            atomic_json(directory / "control.json", state)
            return True

    def commit(self, run_id, attempt_id, generation, manifest, assignment_is_current=None):
        directory = self.run(run_id)
        if manifest.get("run_id") != run_id or manifest.get("attempt_id") != attempt_id or manifest.get("generation") != generation:
            raise ValueError("Result identity mismatch")
        # Complete files precede the immutable manifest, which precedes pointer.
        result_path = directory / "attempts" / canonical_id(attempt_id) / "result.json"
        with record_lock(directory):
            state = self.control(run_id)
            if state.get("result"):
                return state["result"] == str(result_path.relative_to(directory)).replace("\\", "/")
            if not self.owns(state, attempt_id, generation) or state.get("cancel_requested"):
                return False
            if assignment_is_current is not None and not assignment_is_current():
                return False
            self.validate_result(run_id, manifest)
            atomic_json(result_path, manifest)
            state.update(result=str(result_path.relative_to(directory)).replace("\\", "/"),
                status="completed", finished_at=int(time.time() * 1000))
            atomic_json(directory / "control.json", state)
            return True

    def validate_result(self, run_id, manifest):
        if manifest.get("schema") != SCHEMA or not manifest.get("files"):
            raise ValueError("Result has no validated files or unsupported schema")
        prefix = "attempts/" + canonical_id(manifest["attempt_id"]) + "/"
        for asset in manifest["files"]:
            if not asset["path"].startswith(prefix):
                raise ValueError("Result file is outside its attempt")
            path = safe_path(self.run(run_id), asset["path"])
            if not path.is_file() or path.stat().st_size != asset["size"] or asset["size"] <= 0:
                raise ValueError("Missing or invalid published output: " + str(path))
            with path.open("rb") as handle:
                checksum = hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(handle.read()).hexdigest()
            if checksum != asset["sha256"]:
                raise ValueError("Published output hash mismatch: " + str(path))

    def result(self, run_id):
        state = self.control(run_id)
        if not state.get("result"):
            return None
        manifest = read_json(safe_path(self.run(run_id), state["result"]))
        if manifest.get("run_id") != run_id or not self.owns(state, manifest.get("attempt_id"), manifest.get("generation")):
            raise ValueError("Accepted result identity mismatch")
        self.validate_result(run_id, manifest)
        return manifest
