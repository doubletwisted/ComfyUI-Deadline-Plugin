# Native queue administration

Deploy this complete custom-node checkout to the ComfyUI installations used by the farm, then deploy its Deadline render plugin:

```powershell
.\scripts\deploy_deadline_plugin.ps1 -RepositoryPath '\\FILESERVER\DeadlineRepository'
```

The deploy script backs up the repository plugin. Configure **ComfyUI Installation Paths** in Deadline Monitor as usual. Normal native queue tasks launch an owned ComfyUI with Deadline's GPU affinity, verify outputs, publish the result and stop the process before the task completes. They need no scheduled session owner or additional Worker. Worker accounts need the workflow's models and custom nodes.

Set **Native Queue Shared Storage** (`NativeQueueRoot`) in Deadline's ComfyUI plugin configuration to an off-host UNC folder accessible for read/write by submitters and Workers, for example `\\FILESERVER\ComfyQueue`. A new submitter reads this site setting at startup and initializes the shared protocol. `COMFY_DEADLINE_NATIVE_ROOT` overrides the site root for isolated tests. Existing local `deadline_native_settings.json` takes precedence, so restart after changing site settings or update that file deliberately.

The local file retains host administration fields `enabled`, `root`, `scope` and `plugin_directory`. They are absent from the artist sidebar. `enabled:false` disables submission without disabling ordinary local Run. The durable history scope is retained to keep existing runs accessible; new installations use the real Deadline user. Jobs use `deadlinecommand -GetCurrentUserName` for ownership independently of that history scope. Changing root or scope is blocked while accepted or unconfirmed runs remain active.

Pool, group, priority, task timeout and the optional Worker restriction save from the sidebar. An empty restriction lets Deadline choose. Each workflow is one independent one-task job; native batch count supplies parallelism. Offline or busy destinations queue normally. Explicit invalid restrictions block submission.

## Optional warm sessions

Install a persistent session owner only on a Worker/GPU dedicated to ComfyUI. It is a process cache and does not reserve a Worker for an artist.

```powershell
.\scripts\install_native_session.ps1 `
  -Worker GPU-WORKER `
  -GpuUuid GPU-REPLACE-WITH-ACTUAL-UUID `
  -ComfyRoot C:\ComfyUI_windows_portable\ComfyUI `
  -StateRoot C:\ComfyNative\GPU-WORKER `
  -BrokerPort 8202 -ComfyPort 8198 -IdleSeconds 600 -ExclusiveWorker
```

Configure `NativeWorkerSessions` in the Deadline plugin as JSON keyed by Worker name, with its loopback `broker` and NVIDIA `gpu_uuid`. A Worker `ComfyNativeSession` extra-info entry is an optional fallback. Ports and state roots must be unique. The limited interactive logon task uses the installing account; that account must remain logged on with NAS/model access. The Windows job object terminates owned GPU children if their owner dies. Borrowed artist processes are never adopted or killed.

Normal task-owned execution stops the child at each task boundary. The persistent-owner installer requires an exclusive ComfyUI GPU for model-cache warmth.

## Rendering, warmth and reservation

Normal rendering pays startup/model loading for each job and releases the GPU when finished. An optional exclusive owner retains its process and model cache until its idle timeout; Deadline still schedules each independent job.

Actual reservation requires a Deadline session job whose held tasks execute the session's runs themselves. Separate render jobs cannot run on a Worker occupied by a holding task. This second execution path is not implemented or advertised in the sidebar. Adding it requires a durable session claim, cancellation, crash recovery and expiry protocol; keeping a process alive is not a substitute.

## Compatibility and rollback

Native adapters are currently guarded for ComfyUI **0.39.0 / frontend 1.53.10**, English UI, Windows portable Python and NVIDIA GPUs. Validation used Deadline **10.4.2.2** and SMB. Install the whole checkout, including `web`, `plugins` and `scripts`; Python wheel assets are not a supported deployment. Restart the backend and refresh the browser after updating.

The deployment copies `ComfyUI.py`, `ComfyUI.param`, `native_queue_task.py`, `native_queue_assignment.py`, `farm_protocol.py` and `farm_session_owner.py`. Production repository deployment is an administrator operation. Isolated tests can use a private `plugin_directory` without updating the production plugin.

To roll back native submission, set `enabled:false` in the local settings and restart. Restore the repository plugin backup if reverting farm helpers. Preserve shared run records for recovery; do not change root/scope while runs are active.

## Results, Stop, and retry

Native Jobs supports viewing/downloads, opening the readable workflow, and Add to current workflow. Validated outputs retain their image/video/audio groups and live under an isolated `deadline-native/<run>/attempts/...` namespace. A rebuildable copy in the submitting ComfyUI's output folder makes native LoadImage/Video/Audio actions work normally. Deleting that local copy is safe; reconciliation restores it from the share. Keep sufficient local disk capacity for this cache.

Stop first records durable cancellation intent, then suspends Deadline scheduling and stops only that attempt's Worker prompt. It becomes terminal after stop is confirmed. Completion and cancellation serialize under the same per-run lock. The supported frontend groups cancelled jobs under its Failed filter; the adapter labels their rows **Cancelled**. Clearing terminal history hides records rather than deleting their snapshots or files.

For optional persistent owners, an externally suspended task can remain in progress while its prompt stop cannot be confirmed. Reconciliation preserves ownership until cancellation is acknowledged; inspect the owner and Deadline reports before administrative recovery.

**Retry on Deadline** appears in a terminal farm job's native context menu. It creates a new run from the stored snapshot and retained inputs, preserving resolved seeds. Editing the current graph does not change that snapshot. Repeating a request after a lost transport response reconciles the same submission key; another deliberate Run or successful Retry creates another run. An uncertain submission is retained for reconciliation and is never blindly resubmitted.

## Storage and boundaries

The share contains a versioned `store.json`, request-key mappings, and `runs/<UUID>` folders. A run has immutable submission/prompt/workflow/input manifests, a Deadline receipt, cancellation/control state, and isolated attempt/result files. Windows byte-range locks and same-directory atomic replacement protect records. Result publication verifies generation, attempt ownership, exact Deadline assignment start ticks, file paths, sizes, and hashes before updating the accepted-result pointer. No SQLite database is placed on SMB.

Windows UNC record reads use brief exclusive, unbuffered opens to prevent a cached SMB handle from reading a replaced record. Artist and Worker accounts need Modify access, including delete/rename permission, on the native queue folder. Deploy the updated `farm_protocol.py` to both ComfyUI and the Deadline plugin together; changing global SMB caching settings is unnecessary.

There is no automatic retention deletion. Back up the share and establish an administrative retention policy for terminal runs; do not remove active records or files used by retained retries. Local session input/output directories likewise need a retention policy after tasks have released their reservations.

Supported media loaders are listed in `deadline_submit.py` (`INPUT_LOADER_FIELDS` / `ABSOLUTE_FILE_LOADER_FIELDS`). Individual image/video/audio files are staged, including native `[output]`/`[temp]` references. Directory loaders, arbitrary custom file dependencies, browser-interactive nodes, dynamic switches, dynamic output prefixes, partial graphs, and workflows containing Submit to Deadline require adaptation or the existing node-based path. Workflows containing Distributed collectors or distributed upscalers must use local Run; native Deadline routing rejects them before browser worker dispatch. Fixed supported switches are resolved by the existing worker preflight. Workers must have matching node/model/environment dependencies; missing dependencies and missing files produce task diagnostics.

Browser Comfy API credentials are removed from shared submission metadata. Provision authenticated service credentials on Workers independently. This extension uses the existing trusted ComfyUI/Deadline/SMB access model; user scope separates history but is not a new authorization system.

## Rollback and verification

Set `enabled:false` in the local settings and restart to stop new farm submissions. Accepted jobs and controls remain durable. Stop optional warm owners only when their Workers are idle, and retain shared records for history and retries. Restore the repository plugin backup after native tasks finish if reverting deployment. Stopping an owner terminates its owned child; borrowed artist processes remain separate.

See [the validation record](NATIVE_QUEUE_VALIDATION.md) for live evidence and remaining validation limits. M0 fixture scripts are opt-in and are not imported by production registration. No changes to ComfyUI core, installed frontend files, or Distributed are required.
