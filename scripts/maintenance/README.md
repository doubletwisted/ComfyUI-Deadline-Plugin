# Deadline Maintenance Scripts

These scripts are publishable templates for syncing ComfyUI installs and model files through Deadline maintenance jobs.

## Scripts

- `submit_comfy_sync.py` submits maintenance jobs using Deadline's `DeadlineCommand` plugin.
- `ComfyUISync.py` mirrors a shared ComfyUI portable install to local Worker storage.
- `ComfyModelsSync.py` syncs model files from a shared model list to local Worker storage.

## Typical Setup

1. Copy this folder to shared storage visible to all Workers.
2. Edit the default paths in the scripts, or set environment variables on Workers.
3. Submit maintenance jobs from a machine with `deadlinecommand` available.

```powershell
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type both
```

## Useful Overrides

`ComfyUISync.py`:

- `COMFY_SYNC_SOURCE`: shared ComfyUI portable source path.
- `COMFY_SYNC_DEST`: local Worker destination path.

`ComfyModelsSync.py`:

- `COMFY_MODELS_SOURCE`: shared model source path.
- `COMFY_MODEL_LIST`: text file containing model paths to sync.

`submit_comfy_sync.py`:

- `--type installation`: submit only ComfyUI install sync.
- `--type models`: submit only model sync.
- `--type both`: submit both jobs.
- `--allowlist`: comma-separated Worker allowlist.
- `--pool`, `--group`, `--region`: Deadline routing options.
