"""Submission readiness from saved settings and a read-only farm snapshot."""
import os
import re


def off_host_root(value):
    if not isinstance(value, str) or not re.match(r"^\\\\[^\\/]+\\[^\\/]+", value):
        return False
    host = value.split("\\")[2].lower()
    return host not in {"localhost", "127.0.0.1", "::1", os.environ.get("COMPUTERNAME", "").lower()}


def readiness(settings, snapshot):
    checks = []
    def check(identity, label, ok, message):
        checks.append({"id": identity, "label": label, "ok": bool(ok), "message": message})

    check("enabled", "Farm submission", settings.get("enabled") is True,
        "Farm submission is enabled." if settings.get("enabled") else "The farm queue is disabled in the host configuration.")
    storage = snapshot.get("storage", {})
    root_ok = off_host_root(settings.get("root")) and bool(str(settings.get("scope", "")).strip())
    check("storage", "Shared storage", root_ok and storage.get("connected"),
        storage.get("message", "Shared storage has not been checked.") if root_ok else "Choose an off-host shared folder and a user scope.")
    check("repository", "Deadline repository", snapshot.get("connected"),
        snapshot.get("message", "Deadline repository has not been checked."))
    check("plugin", "Native Deadline plugin", snapshot.get("plugin_available"),
        "Native plugin files are installed." if snapshot.get("plugin_available") else "Install the native ComfyUI plugin before submitting.")
    if snapshot.get("configuration_error"):
        check("session_configuration", "Worker session configuration", False, snapshot["configuration_error"])

    known = {worker["name"].lower(): worker for worker in snapshot.get("workers", [])}
    selected = {}
    for name in str(settings.get("workers", "")).split(","):
        if name.strip():
            selected.setdefault(name.strip().lower(), name.strip())
    workers = []
    for key, name in selected.items():
        worker = known.get(key)
        reasons = []
        if worker is None:
            reasons.append("not found in the repository")
        else:
            name = worker["name"]
            if not worker.get("enabled"):
                reasons.append("disabled in Deadline")
            mode = str(worker.get("scheduling_mode", "AllJobs")).strip().lower()
            scope = str(snapshot.get("deadline_user", settings.get("scope", ""))).strip()
            if mode == "userjobs":
                users = worker.get("user_job_names", [])
                if scope.lower() not in {str(user).strip().lower() for user in users}:
                    reasons.append("does not accept Deadline user " + scope + " (allowed: " + (", ".join(users) or "no users") + ")")
            elif mode == "machinejobs":
                machine_user = str(worker.get("machine_user", "")).strip()
                if not machine_user:
                    reasons.append("Deadline machine user could not be verified")
                elif scope.lower() != machine_user.lower():
                    reasons.append("accepts only Deadline machine user " + machine_user)
            elif mode != "alljobs":
                reasons.append("Deadline scheduling mode " + str(worker.get("scheduling_mode")) + " could not be verified")
            for field in ("pool", "group"):
                value = settings.get(field, "none")
                if value.lower() != "none" and value.lower() not in {str(item).lower() for item in worker.get(field + "s", [])}:
                    reasons.append("outside selected " + field)
            if worker.get("session_error"):
                reasons.append(worker["session_error"])
        workers.append({"name": name, "eligible": not reasons, "status": worker.get("status") if worker else "Missing", "reasons": reasons})
    invalid = [worker["name"] + ": " + ", ".join(worker["reasons"]) for worker in workers if not worker["eligible"]]
    worker_message = "; ".join(invalid) if invalid else f"{len(workers)} selected Worker(s) can receive farm jobs."
    if not workers:
        worker_message = "Deadline chooses a Worker from the selected pool and group."
    check("workers", "Worker restriction", not invalid, worker_message)
    failed = next((item for item in checks if not item["ok"]), None)
    return {"ready": failed is None, "code": failed["id"] if failed else "ready",
        "message": failed["message"] if failed else "Ready to submit to Deadline.", "checks": checks, "workers": workers}
