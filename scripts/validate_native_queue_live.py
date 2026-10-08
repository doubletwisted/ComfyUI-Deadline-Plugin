"""Isolated live durability gate on the current M28 Worker.

Starts with Worker offline, restricts it to a unique test user, submits three
accepted jobs, kills only the CPU test submitter, then verifies durable results.
Run after starting the isolated submitter on 8197 and the session owner on 8202.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm_backend import deadline, parse_ini
from farm_protocol import RunStore, atomic_json, read_json


def request(url, body=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"}), timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode()) from exc


def payload(batch):
    return {"submission_key": str(uuid.uuid4()), "batch_id": batch,
        "prompt": {"1": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
            "2": {"class_type": "EmptySD3LatentImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}},
            "3": {"class_type": "VAEDecode", "inputs": {"samples": ["2", 0], "vae": ["1", 0]}},
            "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "native-test"}}},
        "extra_data": {"extra_pnginfo": {"workflow": {"id": batch, "nodes": [], "links": [], "version": .4}}}}


def run(args):
    store = RunStore(args.root)
    args.root.mkdir(parents=True, exist_ok=True)
    if (args.root / "validation.json").exists():
        raise ValueError("Use a fresh root")
    if deadline("-GetSlaveInfo", args.worker, "SlaveStatus").strip() != "Offline":
        raise ValueError("The test Worker must be offline")
    backup = {key: deadline("-GetSlaveSetting", args.worker, key).strip()
        for key in ("SchedulingMode", "UserJobsModeNames", "ConcurrentTasksLimit")}
    atomic_json(args.root / "worker_settings_before.json", backup)
    scope = "native_queue_live_" + uuid.uuid4().hex[:8]
    jobs, runs = [], []
    try:
        for _ in range(20):
            active = request(args.submitter + "/api/jobs?status=pending,in_progress")["jobs"]
            if not active: break
            time.sleep(1)
        if active: raise ValueError("Finish/cancel previous isolated runs before switching test roots")
        deadline("-SetSlaveSetting", args.worker, "UserJobsModeNames", scope)
        deadline("-SetSlaveSetting", args.worker, "SchedulingMode", "UserJobs")
        deadline("-SetSlaveSetting", args.worker, "ConcurrentTasksLimit", "1")
        settings = request(args.submitter + "/deadline/farm/settings")["settings"]
        request(args.submitter + "/deadline/farm/settings", {**settings, "enabled": True, "root": str(args.root),
            "scope": scope, "pool": "test", "workers": args.worker, "priority": 90})
        batch = str(uuid.uuid4())
        for _ in range(3):
            body = payload(batch)
            accepted = request(args.submitter + "/deadline/farm/submit", body)
            run_id = accepted["prompt_id"]
            # A lost transport acknowledgement retries exactly this durable key.
            assert request(args.submitter + "/deadline/farm/submit", body) == accepted
            runs.append(run_id)
            jobs.append(read_json(store.run(run_id) / "receipt.json")["job_id"])
            atomic_json(args.root / "runs.json", {"runs": runs, "jobs": jobs, "scope": scope})
        identity = request(args.submitter + "/deadline/session")
        assert identity["pid"] != request("http://127.0.0.1:8188/deadline/session")["pid"]
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", "Stop-Process -Id " + str(identity["pid"])], check=True)
        print("Accepted all runs; isolated submitting ComfyUI stopped: " + str(identity["pid"]), flush=True)
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName 'ComfyUI-NativeQueue-WorkerSpike'"], check=True)
        until, previous = time.monotonic() + 240, None
        while time.monotonic() < until:
            states = parse_ini(deadline("-GetJob", ",".join(jobs), "True"))
            status = [states[j]["Status"] for j in jobs]
            if previous != status:
                print("Deadline: " + ", ".join(status), flush=True)
                previous = status
            if all(s == "Completed" for s in status):
                results = [store.result(i) for i in runs]
                if not all(results): raise RuntimeError("Completed without a committed result")
                controls = [store.control(i) for i in runs]
                report = {"runs": runs, "jobs": jobs, "scope": scope, "submitter_pid_stopped": identity["pid"],
                    "worker_pids": [s["worker_pid"] for s in controls], "sessions": [s["session_id"] for s in controls],
                    "result_files": [len(r["files"]) for r in results], "submitter_off_completion": True,
                    "cross_job_warmth": len({s["session_id"] for s in controls}) == 1}
                atomic_json(args.root / "validation.json", report)
                print(json.dumps(report, indent=2), flush=True)
                return report
            if any(s == "Failed" for s in status) or any(args.worker in s.get("BadSlaves", "").split(",") for s in states.values()):
                raise RuntimeError("Deadline excluded/failed the test Worker; inspect task reports")
            time.sleep(3)
        raise TimeoutError("Durable live gate timed out")
    finally:
        if jobs:
            states = parse_ini(deadline("-GetJob", ",".join(jobs), "True"))
            active = [j for j in jobs if states[j]["Status"] == "Active"]
            if active: deadline("-SuspendJob", ",".join(active))
        worker = Path(r"C:\Program Files\Thinkbox\Deadline10\bin\deadlineworker.exe")
        subprocess.run([str(worker), "-shutdown", "-name", args.worker], capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(30):
            if deadline("-GetSlaveInfo", args.worker, "SlaveStatus").strip() == "Offline": break
            time.sleep(1)
        if deadline("-GetSlaveInfo", args.worker, "SlaveStatus").strip() != "Offline":
            raise RuntimeError("Worker not offline; restore settings only after it stops")
        for key, value in backup.items(): deadline("-SetSlaveSetting", args.worker, key, value)
        print("Worker stopped; original settings restored", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--worker", default="M28")
    parser.add_argument("--submitter", default="http://127.0.0.1:8197")
    run(parser.parse_args())
