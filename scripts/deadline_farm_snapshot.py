"""Read-only repository snapshot, executed by Deadline's own Python runtime."""
import json
import os
import re
from urllib.parse import urlsplit
from Deadline.Scripting import RepositoryUtils


def session_mapping(mapping):
    if not isinstance(mapping, dict):
        return None, "Session mapping must be a JSON object" if mapping is not None else None
    if not re.fullmatch(r"GPU-[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", str(mapping.get("gpu_uuid", ""))):
        return None, "Session mapping needs a valid NVIDIA GPU UUID"
    try:
        broker = urlsplit(mapping.get("broker", "http://127.0.0.1:8202"))
        if broker.scheme != "http" or broker.hostname not in {"127.0.0.1", "localhost"} or broker.username or broker.password or broker.path not in {"", "/"} or broker.query or broker.fragment or not 1024 <= (broker.port or 80) <= 65535:
            raise ValueError("Session broker must be a Worker-local loopback HTTP endpoint")
    except (ValueError, TypeError) as exc:
        return None, str(exc)
    return mapping, None


def __main__(*args):
    repository = str(RepositoryUtils.GetRepositoryPath("", False))
    config_error = None
    try:
        plugin_config = RepositoryUtils.GetPluginConfig("ComfyUI")
        sessions = json.loads(plugin_config.GetConfigEntryWithDefault("NativeWorkerSessions", "{}"))
        if not isinstance(sessions, dict):
            raise ValueError("NativeWorkerSessions must be a JSON object")
        sessions = {str(name).lower(): mapping for name, mapping in sessions.items()}
    except Exception as exc:
        sessions, config_error = {}, str(exc)
    settings = {str(item.SlaveName): item for item in RepositoryUtils.GetSlaveSettingsList(True)}
    workers = []
    for info in RepositoryUtils.GetSlaveInfos(True):
        name = str(info.SlaveName)
        item = settings.get(name)
        mapping = sessions.get(name.lower())
        error = None
        if mapping is None and item:
            try:
                extra = str(item.GetSlaveExtraInfoKeyValueWithDefault("ComfyNativeSession", ""))
                mapping = json.loads(extra) if extra else None
            except Exception as exc:
                error = str(exc)
        mapping, mapping_error = session_mapping(mapping)
        error = error or mapping_error
        mapped = mapping is not None
        workers.append({"name": name, "host": str(info.HostName), "status": str(info.SlaveStatus),
            "enabled": bool(item.SlaveEnabled) if item else False,
            "scheduling_mode": str(item.SlaveSchedulingMode) if item else "Unknown",
            "user_job_names": [str(user) for user in item.SlaveUserJobsModeNames] if item else [],
            "machine_user": str(info.MachineUserName),
            "pools": [str(p) for p in item.SlavePools] if item else [],
            "groups": [str(g) for g in item.SlaveGroups] if item else [],
            "gpu_model": str(mapping.get("gpu_name", "")) if mapped else "",
            "display_adapter": str(info.MachineVideoCard),
            "gpu_affinity": [int(g) for g in item.SlaveGpuAffinity] if item and item.SlaveOverrideGpuAffinity else [],
            "session_configured": mapped, "session_error": error,
            "gpu_uuid": str(mapping.get("gpu_uuid", "")) if mapped else "",
            "broker": str(mapping.get("broker", "http://127.0.0.1:8202")) if mapped else "",
            "managed_local": bool(item.GetSlaveExtraInfoKeyValueWithDefault("ComfyNativeManaged", "")) if item else False})
    plugin_parent = str(args[0]) if args and str(args[0]).strip() else os.path.join(repository, "custom", "plugins")
    plugin = os.path.join(plugin_parent, "ComfyUI")
    available = all(os.path.isfile(os.path.join(plugin, name)) for name in ("ComfyUI.py", "ComfyUI.param", "native_queue_task.py", "native_queue_assignment.py", "farm_protocol.py", "farm_session_owner.py"))
    print("COMFY_NATIVE_FARM=" + json.dumps({"repository": repository,
        "pools": sorted(str(p) for p in RepositoryUtils.GetPoolNames()),
        "groups": sorted(str(g) for g in RepositoryUtils.GetGroupNames()),
        "workers": sorted(workers, key=lambda w: w["name"].lower()),
        "native_root": RepositoryUtils.GetPluginConfig("ComfyUI").GetConfigEntryWithDefault("NativeQueueRoot", ""),
        "plugin_available": available, "plugin_directory": plugin_parent, "configuration_error": config_error}))
