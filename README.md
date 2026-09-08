# ComfyUI Deadline Plugin

Submit ComfyUI jobs to Thinkbox Deadline from inside ComfyUI.

Quick demo:

<p align="center">
  <a href="https://youtu.be/NFmIvEoEPiU">
    <img src="https://img.youtube.com/vi/NFmIvEoEPiU/maxresdefault.jpg" alt="ComfyUI x Deadline demo" />
  </a>
</p>

This plugin adds two nodes:

- `Submit to Deadline` sends the current workflow to the farm.
- `DeadlineSeed` gives each Deadline variation a predictable seed.

The submitter does not render the workflow locally. It only packages the job and sends it to Deadline.

## Install

Clone this into `ComfyUI/custom_nodes`:

```bash
git clone https://github.com/doubletwisted/ComfyUI-Deadline-Plugin.git
```

Restart ComfyUI.

Then deploy the Deadline plugin:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1
```

The deploy script asks Deadline where the repository lives by running `deadlinecommand -GetRepositoryPath`, then copies `plugins/ComfyUI` into `custom/plugins/ComfyUI`.

You can also point it at the repo yourself:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1 -RepositoryPath "\\YOUR-SERVER\Repository\custom\plugins"
```

In Deadline Monitor, set the ComfyUI plugin's `ComfyUI Installation Paths`. Put one portable ComfyUI root per line:

```text
C:\ComfyUI_windows_portable
D:\Apps\ComfyUI
\\YOUR-SERVER\software\ComfyUI
```

Workers try those paths in order and use the first one that contains `ComfyUI\main.py` and `python_embeded\python.exe`.

## Reusing a worker's GUI session

Set these Deadline worker extra-info keys for each ComfyUI worker: `ComfyUIApiUrl` (for example `http://127.0.0.1:8188`) and `ComfyUILaunchGpuUuid` (an NVIDIA `GPU-...` UUID). The configured local endpoint is verified before a job queues its own prompt behind existing GUI work. Verification prefers the plugin's `/deadline/session` identity response and matching ComfyUI root; for an older already-running session, Windows port ownership, executable path, PID, and command line must match the configured installation. An unrelated service that happens to own the port is never reused. Deadline tracks only that prompt and never stops the GUI backend. If the endpoint is absent, fallback starts only on the configured port and resolves the configured NVIDIA UUID. The older `Per-Worker ComfyUI Endpoints` JSON setting remains a fallback for existing configurations.

For a verified reused endpoint, task inputs are copied into an isolated `input/deadline/<submission-id>` subtree and recorded outputs are copied to the job output directory.

## Use It

Add `Submit to Deadline` to your workflow, set `output_directory` to a path the farm can see, and run the workflow in ComfyUI.

Use `batch_count` for how many variations you want. Use `chunk_size` for how many variations a Deadline task should process before it finishes. These are variations, not animation frames.

If you want seeds to change per variation, use `DeadlineSeed`. A base seed of `1000` becomes:

```text
variation 0 -> 1000
variation 1 -> 1001
variation 2 -> 1002
```

## Input Files

Normal ComfyUI loader nodes copy pasted or uploaded files into `ComfyUI/input`. That folder usually exists only on the machine where you submitted the job, so the plugin stages those referenced files next to your output folder:

```text
<output parent>\input\
```

Workers start ComfyUI with that folder as `--input-directory`.

Only files used by the submitted prompt are copied. If a file already exists and is identical, it is reused. If the name collides with a different file, the plugin gives the staged copy a unique suffix.

Absolute path loader nodes are left alone. Those paths must already be valid on the farm.

## Workflow Metadata

Deadline gets both files:

- `prompt_to_execute.json` is the API prompt the worker renders.
- `workflow.json` is the normal ComfyUI workflow with node positions.

The worker writes the normal workflow metadata back into the output image when ComfyUI provides it. So dragging the finished image back into ComfyUI should reopen the readable graph, not the ugly API-format graph.

## Headless-safety preflight

Every variation is checked on the selected Deadline worker after input staging, seed expansion, and fixed-switch resolution. The worker:

- validates every `class_type` against that worker's live `/object_info`;
- rejects missing staged inputs and known browser-interactive nodes;
- removes fixed `ImpactSwitch`, `LatentSwitch`, and `SEGSSwitch` pass-through nodes by reconnecting consumers to the selected upstream input;
- sends the complete submitted `extra_pnginfo.workflow` to metadata-aware nodes;
- verifies that ComfyUI history reports the expected output nodes and that every reported file exists;
- turns prompt-validation errors, execution exceptions, early process exits, HTTP timeouts, missing output, and Deadline task timeouts into task failures.

Dynamic/connected switch selections are not rewritten. Browser-driven chooser/picker/preview-bridge nodes (`FL_ImagePicker`, `easy imageChooser`, `ImageChooser`, `PreviewChooser`, `PreviewBridge`, and `ImpactPreviewBridge`) are rejected because a farm render has no user to answer them. Other nodes using `PROMPT`, `DYNPROMPT`, `UNIQUE_ID`, or `EXTRA_PNGINFO` remain supported when the worker has the node and the submission includes full workflow metadata. Unknown third-party interactive nodes cannot be identified from `/object_info` alone; add their exact `class_type` to `KNOWN_UI_DEPENDENT_NODE_TYPES` after confirming that they wait for frontend state.

## Maintenance Scripts

There are sanitized Deadline maintenance-job templates in `scripts/maintenance`.

Copy them somewhere your workers can reach, edit the default paths, then submit sync jobs with:

```powershell
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type both
```

You can also run only one side:

```powershell
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type installation
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type models
```

## Notes

- This targets portable Windows ComfyUI workers.
- Deadline handles render timeouts. Set those in Deadline Monitor.
- `/deadline/session` is identity-only and is used to prove endpoint ownership. `ComfyUI-Deadline-Distributed` continues to own its execution routes.

Run all regression tests with `python -m unittest discover -s tests -v`.
