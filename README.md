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
- The old fake `/deadline/*` API routes are gone from this plugin. If you use `ComfyUI-Deadline-Distributed`, it owns its own routes.
