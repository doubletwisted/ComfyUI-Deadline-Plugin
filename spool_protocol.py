"""Spool Deadline protocol 2. Copied beside the worker; stdlib only."""
import copy
import hashlib
import json
import ntpath
import os
import re
import shutil
import uuid
from pathlib import Path

SCHEMA = 2
SAVERS = {"SaveImage": {"images"}, "PreviewImage": {"images"}, "SaveVideo": {"images", "videos"}, "VHS_VideoCombine": {"gifs"}}


def contained(root, relative):
    relative = os.fspath(relative)
    if not isinstance(relative, str) or not relative or ntpath.splitdrive(relative)[0] or relative.startswith(("/", "\\")):
        raise ValueError("Farm paths must be relative")
    for part in relative.replace("\\", "/").split("/"):
        if part and (part == ".." or part.rstrip(" .") != part or any(c in part for c in ':\x00\r\n"') or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise ValueError("Unsafe farm path component")
    root = Path(root).resolve()
    path = (root / relative.replace("\\", "/")).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("Farm path escapes configured storage")
    return path


def checksum(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        with pending.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def semantic_prompt(prompt):
    result = copy.deepcopy(prompt)
    for node in result.values():
        node.pop("is_changed", None)
    return result


def saver_prefix(package, execution_id, node_id):
    for identifier in (package["attemptId"], execution_id):
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier):
            raise ValueError("Invalid Spool execution identifier")
    node_folder = hashlib.sha256(node_id.encode()).hexdigest()[:16]
    return "spool/" + package["attemptId"] + "/" + execution_id + "/" + node_folder + "/out"


def prepare_execution(package, prompt, input_root, variation_index):
    if package["schemaVersion"] != SCHEMA or variation_index != package["variationIndex"]:
        raise ValueError("Spool task does not match its explicit variation assignment")
    if semantic_prompt(prompt) != semantic_prompt(package["stagedPrompt"]):
        raise ValueError("Worker transformations changed the staged Spool prompt")
    for asset in package["assets"]:
        path = contained(input_root, asset["staged"])
        if path.stat().st_size != asset["byteSize"] or checksum(path) != asset["sha256"]:
            raise ValueError("Staged Spool dependency changed")
        if prompt[asset["nodeId"]]["inputs"][asset["input"]] != asset["staged"]:
            raise ValueError("Staged input reference does not match its dependency")
    execution_id = uuid.uuid4().hex
    saver_prefix(package, execution_id, "")
    observed = copy.deepcopy(prompt)
    for node_id, node in observed.items():
        if node["class_type"] in SAVERS and "filename_prefix" in node["inputs"]:
            node["inputs"]["filename_prefix"] = saver_prefix(package, execution_id, node_id)
    return {"executionId": execution_id, "prompt": observed, "variationIndex": variation_index}


def finalize(package, execution, prompt_id, history, roots, worker, job_id):
    saver_prefix(package, execution["executionId"], "")
    status = history.get("status", {})
    if not status.get("completed") or status.get("status_str") != "success":
        raise ValueError("Spool requires successful exact-prompt history")
    if semantic_prompt(history.get("prompt", [None, None, {}])[2]) != semantic_prompt(execution["prompt"]):
        raise ValueError("Worker hooks changed the verified Spool request")
    relative = "spool-receipts/" + package["attemptId"] + "/" + execution["executionId"]
    target = contained(package["resultRoot"], relative)
    target.mkdir(parents=True, exist_ok=True)
    receipt_path = contained(target, "receipt.json")
    if receipt_path.is_file():
        existing = json.loads(receipt_path.read_text(encoding="utf-8"))
        verify(package, existing, package["resultRoot"])
        return existing
    artifacts = []
    for node_id, node in execution["prompt"].items():
        for group in sorted(SAVERS.get(node["class_type"], ())):
            for index, record in enumerate(history.get("outputs", {}).get(node_id, {}).get(group, [])):
                name = record["filename"]
                if Path(name).name != name or "\\" in name or "/" in name:
                    raise ValueError("Invalid worker artifact filename")
                kind = record.get("type", "output")
                if kind not in roots:
                    raise ValueError("Unknown worker storage type")
                source = contained(roots[kind], str(Path(record.get("subfolder", "")) / name))
                if "filename_prefix" in node["inputs"]:
                    namespace = contained(roots[kind], saver_prefix(package, execution["executionId"], node_id).rsplit("/", 1)[0])
                    if not source.is_relative_to(namespace):
                        raise ValueError("Worker artifact belongs to another execution")
                artifact_id = uuid.uuid4().hex
                destination = target / (artifact_id + source.suffix.lower())
                pending = destination.with_name(destination.name + ".partial")
                with source.open("rb") as reader, pending.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
                digest = checksum(source)
                if checksum(pending) != digest:
                    raise ValueError("Worker artifact copy failed integrity verification")
                pending.replace(destination)
                artifacts.append({"artifactId": artifact_id, "nodeId": node_id, "group": group, "index": index,
                                  "storageId": "spool-results", "path": destination.relative_to(Path(package["resultRoot"]).resolve()).as_posix(),
                                  "byteSize": destination.stat().st_size, "sha256": digest})
    selection = package["result"]
    primary = next((a for a in artifacts if all(a[k] == selection[k] for k in ("nodeId", "group", "index"))), None)
    if primary is None:
        raise ValueError("Successful worker history omitted the selected artifact")
    receipt = {k: package[k] for k in ("schemaVersion", "projectId", "clipId", "takeId", "attemptId", "fingerprint", "submissionId", "variationIndex")}
    receipt.update({"executionId": execution["executionId"], "promptId": prompt_id, "jobId": job_id, "worker": worker,
                    "outcome": "success", "observedPrompt": history["prompt"][2], "history": history,
                    "artifacts": artifacts, "primaryArtifactId": primary["artifactId"]})
    atomic_json(receipt_path, receipt)
    return receipt


def verify(package, receipt, result_root):
    saver_prefix(package, receipt.get("executionId"), "")
    if receipt.get("schemaVersion") != SCHEMA:
        raise ValueError("Unsupported farm receipt version")
    for key in ("projectId", "clipId", "takeId", "attemptId", "fingerprint", "submissionId", "variationIndex"):
        if receipt.get(key) != package[key]:
            raise ValueError("Farm receipt belongs to another request: " + key)
    if receipt.get("outcome") != "success" or not receipt.get("promptId") or not receipt.get("executionId"):
        raise ValueError("Farm receipt lacks execution identity or success")
    history = receipt.get("history", {})
    if history.get("status", {}).get("status_str") != "success" or not history.get("status", {}).get("completed"):
        raise ValueError("Farm receipt lacks completion evidence")
    expected = copy.deepcopy(package["stagedPrompt"])
    for node_id, node in expected.items():
        if node["class_type"] in SAVERS and "filename_prefix" in node["inputs"]:
            node["inputs"]["filename_prefix"] = saver_prefix(package, receipt["executionId"], node_id)
    if semantic_prompt(receipt.get("observedPrompt", {})) != semantic_prompt(expected) or semantic_prompt(history.get("prompt", [None, None, {}])[2]) != semantic_prompt(expected) or history["prompt"][1] != receipt["promptId"]:
        raise ValueError("Farm observed prompt differs from the permitted execution copy")
    artifacts = receipt["artifacts"]
    if len({a["artifactId"] for a in artifacts}) != len(artifacts):
        raise ValueError("Duplicate artifact identity")
    reported = {(node_id, group, index) for node_id, node in expected.items() for group in SAVERS.get(node["class_type"], ())
                for index, record in enumerate(history.get("outputs", {}).get(node_id, {}).get(group, []))}
    if {(a["nodeId"], a["group"], a["index"]) for a in artifacts} != reported:
        raise ValueError("Farm receipt omitted or invented a reported artifact")
    for artifact in artifacts:
        if artifact["storageId"] != "spool-results":
            raise ValueError("Unknown farm storage ID")
        path = contained(result_root, artifact["path"])
        namespace = contained(result_root, "spool-receipts/" + package["attemptId"] + "/" + receipt["executionId"])
        if not path.is_relative_to(namespace) or path.stat().st_size != artifact["byteSize"] or checksum(path) != artifact["sha256"]:
            raise ValueError("Farm artifact integrity or containment failed")
    primary = next((a for a in artifacts if a["artifactId"] == receipt["primaryArtifactId"]), None)
    if not primary or any(primary[k] != package["result"][k] for k in ("nodeId", "group", "index")):
        raise ValueError("Farm primary differs from explicit result selection")
    return primary
