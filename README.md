# ComfyUI Deadline Plugin

Submit ComfyUI workflows to Thinkbox Deadline.

## Features

- Submit the current ComfyUI API prompt directly to Deadline
- Submit-only local execution: normal output nodes are not rendered on the submitter
- Deadline variation jobs using `batch_count` and `chunk_size`
- Deterministic seed variation through the `DeadlineSeed` node
- Stages referenced default ComfyUI input assets beside the output directory
- Stores the normal ComfyUI `workflow.json` with node placement in the Deadline job files
- Launches isolated portable Windows ComfyUI worker instances
- Preserves compatibility flags used by `ComfyUI-Deadline-Distributed`

## Installation

### ComfyUI

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/doubletwisted/ComfyUI-Deadline-Plugin.git
```

Restart ComfyUI after installing or updating.

### Deadline

Deploy `plugins/ComfyUI/` into your Deadline Repository `custom/plugins/` directory, then restart Deadline services or reload the repository plugin.

For render-farm maintenance, this repo includes publishable templates in `scripts/maintenance`. Copy those scripts to a shared path reachable by Workers, update their default paths or pass overrides, then submit them as Deadline maintenance jobs:

```powershell
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type both
```

The maintenance submitter can submit the ComfyUI install sync, the model sync, or both:

```powershell
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type installation
python \\YOUR-SERVER\share\scripts\maintenance\submit_comfy_sync.py --type models
```

For Deadline repository plugin deploys, this repo includes a direct deploy helper:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1
```

The deploy script discovers the repository with `deadlinecommand -GetRepositoryPath`. You can also pass either the repository root or the custom plugins folder explicitly:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\deploy_deadline_plugin.ps1 -RepositoryPath "\\YOUR-SERVER\Repository\custom\plugins"
```

In Deadline Monitor, configure the ComfyUI plugin `ComfyUI Installation Paths` setting. Use one portable Windows ComfyUI root per line:

```text
C:\ComfyUI_windows_portable
D:\Apps\ComfyUI
\\YOUR-SERVER\software\ComfyUI
```

Workers try each entry in order after Deadline path mapping and pick the first path containing both `ComfyUI\main.py` and `python_embeded\python.exe`.

## Usage

1. Add `Submit to Deadline` to the workflow.
2. Set a farm-visible `output_directory`.
3. Use `DeadlineSeed` anywhere a seed value should vary between Deadline variations.
4. Run the workflow in ComfyUI.
5. Monitor the submitted job in Deadline Monitor.

`batch_count` is the total number of Deadline variations. `chunk_size` is how many variations a single Deadline task should queue into its worker ComfyUI instance. These are not animation frame ranges; Deadline frames are used internally as variation indices.

For a `DeadlineSeed` base seed of `1000`, variation `0` uses `1000`, variation `1` uses `1001`, and so on. The worker rewrites the queued prompt before execution, so saved image metadata contains the actual seed used. Additional Deadline metadata is written under `extra_pnginfo.deadline`.

Deadline jobs include two workflow files when ComfyUI provides the UI workflow metadata: `prompt_to_execute.json` is the API prompt used by the worker, and `workflow.json` is the standard ComfyUI workflow with node positions. Patched workers embed the standard workflow metadata into outputs, so dropping a generated image back into ComfyUI opens the normal graph layout, not the API prompt format.

## Input Staging

Default ComfyUI upload nodes such as `Load Image`, `Load Audio`, and `Load Video` store files in the local `ComfyUI/input` folder. On submission, this plugin copies referenced input assets to a shared sibling input folder beside the output directory:

```text
<output parent>\input\
```

Workers launch ComfyUI with `--input-directory` pointing at that staged folder. The embedded standard workflow metadata is rewritten to use absolute staged asset paths, so another ComfyUI session on a different machine can reopen the generated image as long as that shared input path is visible there. Missing or invalid referenced input files fail submission before the Deadline job is sent.

Only files referenced by the submitted prompt are copied. Existing identical files are reused; conflicting filenames get a submission suffix. Absolute path loader nodes are left unchanged and must already point to farm-visible storage.

## Notes

- This V2 path targets portable Windows ComfyUI workers.
- Deadline owns render timeout policy; configure timeouts in Deadline Monitor.
- The base plugin no longer registers mock `/deadline/*` routes. Distributed-worker routes remain owned by `ComfyUI-Deadline-Distributed`.
