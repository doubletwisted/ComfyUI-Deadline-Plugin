"""Read administrator-owned ComfyUI storage settings through the Deadline client."""
import json
import ntpath
import os

from Deadline.Scripting import RepositoryUtils, PathUtils


def storage_locations(value):
    roots = []
    for line in value.replace(";", "\n").splitlines():
        path = line.strip()
        if not path:
            continue
        if path.endswith(("\\*", "/*")):
            path = path[:-1]
        if "*" in path or "?" in path or not os.path.isabs(path):
            raise ValueError("Allowed Storage Locations needs one absolute folder or drive pattern per line, e.g. X:\\*.")
        roots.append(RepositoryUtils.CheckPathMapping(path))
    return roots


def __main__(*args):
    config = RepositoryUtils.GetPluginConfig("ComfyUI")
    default = config.GetConfigEntryWithDefault("DefaultOutputDirectory", "").strip()
    roots = storage_locations(config.GetConfigEntryWithDefault("AllowedStorageRoots", ""))
    if default:
        roots.append(RepositoryUtils.CheckPathMapping(default))
    settings = {"output_roots": roots, "input_roots": roots}
    settings["drive_mappings"] = {}
    for root in list(roots):
        drive, tail = ntpath.splitdrive(root)
        drive = drive.upper()
        if len(drive) == 2 and drive[1] == ":" and drive not in settings["drive_mappings"]:
            network_root = PathUtils.GetMappedDriveNetworkPath(drive)
            if network_root:
                settings["drive_mappings"][drive] = network_root
        network_root = settings["drive_mappings"].get(drive)
        if network_root:
            network_path = ntpath.join(network_root, tail.lstrip("\\/"))
            if not any(ntpath.normcase(ntpath.normpath(network_path)) == ntpath.normcase(ntpath.normpath(value)) for value in roots):
                roots.append(network_path)
    print("COMFY_DEADLINE_FARM_SETTINGS=" + json.dumps(settings))


if __name__ == "__main__":
    __main__()
