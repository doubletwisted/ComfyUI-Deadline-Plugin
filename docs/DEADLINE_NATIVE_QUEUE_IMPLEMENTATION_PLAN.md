# Deadline integration through ComfyUI's native queue

Product correction after Opus 5.5 review (2026-10-07): normal tasks use a task-owned ComfyUI process on existing Workers. Persistent owners are optional. Warm model reuse is validated only on exclusive ComfyUI GPUs; shared mode releases the process at task completion. The sidebar no longer installs Workers, scheduled tasks or plugins. Artist reservation remains a separate, unimplemented execution mode. Earlier checked lifecycle gates below describe exclusive-owner validation, not warm reuse on every Worker.

Implementation handoff, 2026-10-06. The implementation is now present, uncommitted. See [setup](DEADLINE_NATIVE_QUEUE.md) and [validation evidence](NATIVE_QUEUE_VALIDATION.md). The starting-point notes below describe the pre-implementation baseline. Checked items have implementation and automated or live evidence; unchecked items identify remaining rollout validation, as detailed in the validation log.

## 1. Agreed behavior and boundaries

- Add **Run (Deadline)** to the existing Run dropdown. Keep the existing Run control, batch count, workflow serialization, and seed/widget callbacks.
- Display local and farm runs in **ComfyUI's existing Jobs panel and history**. Include queued/running/completed/failed/cancelled states, progress, previews, outputs, errors, Stop, and retry.
- A Deadline settings panel may show connection health, configuration, and worker/session information. Do not build a second job list or result browser.
- Submit complete, immutable snapshots of independent runs. Do not split one workflow across machines.
- Keep submission, staging, reconciliation, and controls in Python. Deadline owns farm scheduling; browser code must not choose workers or dispatch prompts to them.
- Accepted runs must continue after the submitting ComfyUI process or computer shuts down. No dedicated graph coordinator machine is required.
- Reuse warm worker sessions and clean up idle owned sessions. Existing artist-owned ComfyUI sessions must never be terminated by the farm plugin.
- Make changes inside this plugin and, where necessary, ComfyUI-Deadline-Distributed. Do not edit ComfyUI core, the installed frontend package, or a frontend source checkout. Runtime adapters shipped inside the plugin are allowed, but must be narrow, guarded, and tested.
- Preserve existing Submit to Deadline/DeadlineSeed nodes, aliases, and the Distributed Collector/Upscale path.

The earlier `work/deadline-farm-concept.html` mockup contains a separate Farm job list. That part is superseded by this plan. It is not an implementation specification.

## 2. Ownership: what “native queue integration” means

The Jobs panel is the shared user interface. It does not have to be the submitting machine's actual execution queue.

1. Normal ComfyUI submission produces a resolved API prompt and readable workflow snapshot.
2. The plugin submits that snapshot as a Deadline job and returns a stable ComfyUI-compatible run ID.
3. A Python adapter exposes local job records plus Deadline-backed records through the API already used by the Jobs panel.
4. Deadline assigns the job to a worker. The worker queues one prompt into **its own** ComfyUI instance.
5. The worker reports scheduling/progress to Deadline and publishes ComfyUI results and metadata to shared storage.
6. The submitting plugin reads those records for display and controls. On restart it reconstructs them without needing an old browser connection or in-memory registry.

Never insert a remote run into the submitting instance's `PromptQueue`: its local executor would render it. Do not create two schedulers by maintaining a local pending list that must continuously feed Deadline. Every accepted independent run must already exist in Deadline.

## 3. Verified starting point

Recheck these before implementation; the workspace and running applications can change.

- Main repository: `C:\AI\ComfyUI_windows_portable4\ComfyUI\custom_nodes\ComfyUI-Deadline-Plugin`, commit `e87701a`, package version `1.0.10`.
- Distributed repository: sibling `ComfyUI-Deadline-Distributed`, commit `62c81b1`.
- ComfyUI: `0.39.0`, commit `b0b743566f65daafc423b4fea8a2fbda94b3384a`.
- Installed frontend: `1.53.10`. Source was inspected at matching tag `v1.53.10`, not frontend `main`.
- Read-only frontend checkout used for research: `C:\Users\DOMINI~1.MIL\AppData\Local\Temp\comfy-deadline-queue-research\frontend-v1.53.10`. It is temporary; fetch the matching tag again if missing.
- On the last check, a ComfyUI instance answered on `http://127.0.0.1:8188`; `/system_stats` reported Python `3.12.8`, embedded Python, Windows. `/deadline/session` also responded.
- `deadlinecommand.exe` was available through `DEADLINE_PATH`; its file version was `10.4.2.2`, and `-GetRepositoryPath` returned `\\NAS01\repository`.
- Supplied PDG reference: `\\10.35.96.10\Repository\custom\plugins\PDGDeadline\PDGDeadline.py`. Verify repository aliases before deploying anything.
- Existing regression suite: **30 tests passed**. These use stubs and do not prove live Deadline scheduling, cancellation, persistence, or GPU reuse. One test intentionally logs a synthetic failure traceback while passing.
- No real farm jobs, worker failures, adapter behavior, or persistent process lifecycle were exercised for this plan. No runtime implementation was changed.

Read the applicable `AGENTS.md`, including `ComfyUI\AGENTS.md`. Inspect current diffs before editing. Use the checkout's Python modules explicitly: an older installed `deadline_submit` package was found to shadow this checkout in an earlier test invocation.

### Existing code to reuse

- `deadline_submit.py`: `DeadlineCommandHelper`, `WorkflowProcessor`, `InputAssetStager`, `DeadlineJobSubmitter`, prompt/readable-workflow asset rewrites, existing seed handling.
- `__init__.py`: current node registration and submit-only prompt handler. The new frontend extension needs `WEB_DIRECTORY`; this repository currently does not export it.
- `plugins/ComfyUI/ComfyUI.py`: endpoint/GPU identity verification, own-prompt tracking, staging for reused endpoints, worker `/object_info` preflight, fixed-switch handling, metadata, history/error extraction, output existence validation, Deadline progress/failure reporting.
- `plugins/ComfyUI/ComfyUI.param`: existing installation and worker endpoint settings.
- `scripts/deploy_deadline_plugin.ps1`: deploys `plugins/ComfyUI` to the Deadline repository; account for any added helper files and worker-side extension installation.
- Distributed `deadline_worker_registration.py`, `deadline_integration_simple.py`, and `worker_monitor.py`: worker identity, registration/heartbeat, and process monitoring concepts.
- Distributed `web/main.js`: existing sidebar registration pattern.

Reuse needs adaptation, not wholesale activation: Distributed currently registers against `COMFY_MASTER_*`, keeps claims in memory, and holds registration jobs alive. Its browser scheduling in `web/executionUtils.js` and execution patches for workflow splitting are not the scheduler for this feature. Preserve their existing behavior.

Read-only integration references in the matching frontend source: `src/components/actionbar/ComfyRunButton/ComfyQueueButton.vue`, `src/scripts/app.ts` (`queuePrompt`), `src/scripts/api.ts` (`apiURL`, `queuePrompt`, cancellation), `src/platform/remote/comfyui/jobs/`, `src/stores/queueStore.ts`, `src/stores/executionStore.ts`, `src/stores/jobPreviewStore.ts`, and `src/composables/queue/useJobMenu.ts`. In ComfyUI, inspect `server.py` (Jobs routes, middleware/application setup, `add_routes`, events), `main.py` (startup and `prompt_worker`), and the pure helpers in `comfy_execution/jobs.py`.

## 4. Scheduling decision and persistent session spike

**Default implementation: one independent run = one Deadline job with one task**, grouped under a batch name for a native multi-run submission. Use `BatchCount=1` and `ChunkSize=1` for this path. Retain the existing node-based variation/batch behavior separately. Deadline priorities, pools, groups, limits, and GPU/Worker affinity govern scheduling.

Do not regenerate resolved seeds on the worker for this new path. The snapshot received from normal frontend submission is the run to execute. Explicit retry uses that same snapshot.

Evaluate these options in milestone M0 and record measurements:

| Option | Scheduling and lifetime | Decision |
| --- | --- | --- |
| One job/task per run, verified reusable worker-local ComfyUI session | Durable independent scheduling; process reuse can work across jobs | Preferred, subject to lifecycle/ownership tests |
| Fixed batch job with all tasks defined before submission, Advanced Deadline plugin | Can retain an application between tasks within a job; runs may still execute on different Workers | Optional later optimization; prove per-run Stop/retry and distribution first |
| Persistent Deadline slot jobs pulling a shared queue | Needs a durable claim/dispatch system and separate resource ownership | Do not use for MVP; adds an unnecessary scheduler unless measured benefits justify it |
| Append tasks to an active Deadline job | Requires reliable dynamic append under farm load | Excluded from MVP; no assumption of reliability |

PDG's transferable pattern is backend submission of work-item specifications and backend result reporting. Interactive PDG still has a local graph coordinator; Submit as Job moves that graph cook to a farm `hython`. Independent fully submitted ComfyUI runs do not need that graph coordinator. SideFX also documents a dynamic task-append race and a one-job-per-work-item workaround. [SideFX Deadline Scheduler](https://www.sidefx.com/docs/houdini/nodes/top/deadlinescheduler.html)

Deadline documents Advanced plugins retaining applications across tasks. That is not proof of application persistence across separate jobs or sandbox teardown. The existing ComfyUI Deadline plugin is Simple, and its distributed keep-alive loop has no idle-drain protocol. [Deadline application plugins](https://docs.thinkboxsoftware.com/products/deadline/10.4/1_User%20Manual/manual/application-plugins.html)

For cross-job warmth, prefer a verified worker-local session owner/helper which survives the individual render task. It coordinates only that machine's process lifetime, not the farm queue. Prove that Deadline task/sandbox cleanup leaves that session alive, and that crashes and shutdowns cannot leave unmanaged GPU processes behind. Do not rely on a detached child surviving by accident.

Session requirements:

- Identify an owned session by Deadline Worker identity, GPU UUID, ComfyUI root, PID, and session UUID. A port number alone is not identity. One active farm prompt per owned GPU/session in MVP.
- Track active task/prompt reservations locally; a heartbeat expiring is not permission to kill an actively rendering process.
- Keep input/output roots stable for a persistent process. Use run/attempt namespaces; do not restart merely to change a per-job `--input-directory` or `--output-directory`.
- A short configurable idle timeout retains warmth; an owned idle session drains and exits afterward. It stops accepting reservations before shutdown, then rechecks active work.
- Deadline task completion releases scheduling capacity. A warm process still holding VRAM must not conflict with the next non-Comfy job: start with exclusive Comfy GPU Workers, or prove a worker-local handoff that frees the session before another application uses that GPU.
- Reused artist endpoints are borrowed. Never kill them or globally interrupt unrelated queued/running prompts. Record that their cleanup policy differs from owned sessions.
- Shared registration data is discovery/diagnostics, not authority to dispatch farm work. Adapt Distributed registration to a durable/local destination; master workstation callbacks must not be required for accepted runs.

## 5. Durable run, submission, and result protocol

Use Deadline for scheduling and a farm-visible share for immutable payloads and result/control metadata. A submitter-local cache/index is rebuildable. Do not place the only registry or cancellation flag in browser storage or local SQLite. Do not put a concurrently written SQLite database on SMB.

Define and version a small JSON protocol, preferably with one shared implementation usable by the submitter and deployed Deadline plugin:

- `run_id`: canonical lowercase UUID; returned as native `prompt_id` and used as the native job ID. Do not expose a Deadline job ID as a native UUID.
- `submission_key`: stable request ID for transport retries of the same snapshot; associate it with a payload hash. Same key with different payload must be rejected.
- `batch_id`, `workflow_id`, submitter/user scope, creation time, readable name, and schema version.
- Exact API prompt, readable workflow, execution options, staging manifest, snapshot hash, and captured configuration. Do not mutate these after preparation.
- Deadline job/task IDs in the accepted receipt and job metadata, with a manifest locator. Records must be discoverable after losing the submitting computer.
- `attempt_id`, generation/fencing number, Deadline Worker identity, session UUID, worker prompt ID, timestamps, and progress sequence.
- A cancellation intent and a pointer to the single accepted result manifest.

Separate deliberate Run clicks are distinct runs even when their snapshot hashes match. Deduplicate transport retries by request key, not all identical workflows by content hash.

Suggested layout under a configurable **off-host** root, not automatically inside Deadline's plugin directory:

```text
<farm-root>/runs/<run_id>/
  submission.json        # immutable snapshot/configuration/hash
  prompt.json
  workflow.json
  inputs/                # or references to existing staged immutable assets
  input_manifest.json
  receipt.json           # accepted Deadline job/task mapping
  control.json           # generation, cancellation, accepted result pointer
  attempts/<attempt_id>/ # isolated progress, errors, preview, outputs, result.json
```

Reuse `InputAssetStager` and its existing supported-loader behavior. Refactor only the pieces needed for a per-run directory and a non-node submission entry point. Keep unsupported directory-loader limitations explicit. Preserve API and readable workflow asset rewrites together. Model files/custom nodes remain a worker environment responsibility; record useful environment fingerprints and report missing requirements.

### Submission and restart recovery

1. Validate request shape, settings, and staging accessibility; write the complete payload atomically before submitting anything runnable.
2. Serialize submissions for the same key. Put the run ID, protocol marker, and manifest locator into Deadline's supported metadata fields.
3. Prefer initial suspended submission, then persist the Job ID mapping and activate the job. Verify these Deadline commands and state transitions on the installed version before depending on them.
4. Acknowledge acceptance only after a recoverable mapping exists and activation is confirmed. Record the receipt before sending the HTTP response.
5. On a submission timeout or lost response, reconcile by the durable key/metadata. Do not blindly call SubmitJob again. Surface an unconfirmed state and prevent a second submission until reconciled.
6. If the submitter crashes before activation, the run was not yet accepted; another connected plugin instance must be able to recover the suspended job. If it crashes after activation but before replying, discover the running job rather than duplicating it.
7. A native batch may be accepted partially. Report accepted IDs and failed/unconfirmed members clearly. Do not claim an entire batch is farm-owned while some runs exist only in a browser loop.

### Result publication and retry fencing

Use a tested server-enforced lock for shared `control.json`, plus atomic file replacement on the same share. On Windows SMB, evaluate OS byte-range locking; test from two different hosts, including process death. Atomic rename alone is not a compare-and-swap or fencing mechanism. If the storage cannot provide the required exclusion, stop and report the exact unmet durability requirement before selecting a different design.

1. Verify the task assignment belongs to this worker/attempt. At attempt start, under the run lock, reject cancellation; if a result is already committed, finish/reconcile without rendering it again. Otherwise increment generation and record the new attempt. Do not let a stale callback reclaim ownership merely because it can still reach the share.
2. Execute into an attempt-specific namespace. Track only the returned worker prompt ID and capture its history, outputs, and errors.
3. Validate and copy outputs before publication. Write an immutable result manifest with normalized node outputs, paths, sizes/hashes as needed, workflow metadata, and attempt identity.
4. Under the lock, publish the result pointer only if this attempt still owns the generation and cancellation has not won. Late superseded attempts may retain diagnostic files but cannot become visible results.
5. Publish the manifest before reporting Deadline task success. If storage is unavailable, do not report successful completion with missing metadata.
6. Test the “result committed, Deadline acknowledgement lost” case. A retry must find that commit and avoid another visible result; reconciliation must make the final status consistent.

The required guarantee is one accepted visible result per run, not exactly-once execution or arbitrary third-party node side effects. Deadline may retry execution. An explicit user Retry creates a **new run ID**, linked to the original, from its stored snapshot/settings with identical resolved seeds. Automatic infrastructure attempts remain under the original run ID.

## 6. Native Jobs API and frontend integration

Implement a plugin-owned Python Jobs adapter. Preferred seam: install narrow aiohttp middleware during custom-node startup, before the application freezes. The inspected startup order creates `PromptServer`, imports custom nodes, then adds routes/starts the server. This is an internal runtime seam, not a verified public remote-job-provider API.

The inspected frontend reads lists and details through `/api/jobs`, via `api.fetchApi('/jobs...')`. Its schema supports the terminal and active states needed here. Inspect the matching source before shaping responses. [Jobs fetchers](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.53.10/src/platform/remote/comfyui/jobs/fetchJobs.ts), [Jobs schema](https://github.com/Comfy-Org/ComfyUI_frontend/blob/v1.53.10/src/platform/remote/comfyui/jobs/jobTypes.ts)

Required behavior:

- `GET /api/jobs`: merge normalized local and farm records, then apply filters, sorting, pagination, and totals to the combined set. Do not concatenate two already paginated pages. Use existing pure normalization helpers where suitable; do not change the underlying executor queue/history.
- `GET /api/jobs/<run_id>`: return the stored submission workflow, complete normalized outputs, status, timestamps, and structured errors for farm IDs. Local IDs use normal behavior.
- `GET /api/jobs/<run_id>/assets`: provide native-compatible asset entries where needed by this frontend; use the same accepted manifest as job detail.
- Native single/batch cancellation endpoints: route farm IDs to the farm controller, local IDs to normal cancellation. Preserve validation, idempotence, and mixed batches.
- Audit actual consumers of `/queue`, `/history`, `/interrupt`, clear/delete, and their `/api` aliases. Support required compatibility views/controls, without changing the real local queue. Do not register duplicate routes over existing handlers.
- Clearing pending jobs cancels the addressed scoped runs. Clearing/deleting history hides records according to the chosen user policy; it must not implicitly delete Deadline jobs or shared output files. Retain terminal receipts/results for the configured retention period even if Deadline purges its own finished jobs.
- Completed media must be available through native view/download URLs. Prefer lazy materialization into a namespaced local output cache where this also makes “Add to workflow” work; otherwise use a plugin-owned safe asset resolver and test every consumer. Never serve an arbitrary supplied filesystem path.
- Scope records to the current configured user/workspace policy. Do not expose every artist's farm jobs by default. Preserve that identity across restart and document how another authorized workstation can recover the same scope.
- Keep local operations available if Deadline is unreachable. Cache farm records with a visible stale/connection indicator rather than deleting rows or inventing completion/failure.
- Backend polling must be batched, bounded, and off the aiohttp event loop. Start with cached state refresh around 2 seconds while active and slower idle polling; measure on the actual farm. Do not spawn one Deadline subprocess per row or browser poll.

Native lifecycle bridging needs its own prototype:

- Send plugin-specific updates keyed by run ID, attempt generation, and sequence; map them into the native job progress/preview state through a small frontend compatibility adapter.
- Do not indiscriminately replay remote `execution_start`, `executing`, `executed`, or legacy binary previews as if all Workers were one local executor. The inspected frontend has per-job progress maps **and** global active-job/canvas state. Its frame coalescing also needs concurrency testing.
- Preserve foreground workflow highlighting and local execution while two remote workflows use identical node IDs. Do not attach one worker's preview/error to another run or the currently open unrelated graph.
- Restore current progress/preview after reconnect from the durable latest state. Live preview loss must not prevent execution or final result publication. Throttle preview files/events and bound retention.
- Check native active count, workflow filters, and lists with more than 200 active runs; the inspected fetcher requests a maximum active page of 200. Do not silently mistake the loaded page for the farm's full queue.

### Run menu and retry

Add a web extension under this repository and export `WEB_DIRECTORY` from `__init__.py`. Use supported extension settings/commands/sidebar hooks first. The inspected `ComfyQueueButton.vue` constructs its Run mode menu internally; extension menu commands do not automatically add a dropdown item. Its exact dropdown integration must therefore be proved in M0.

- Keep `app.queuePrompt`'s normal widget callbacks and graph serialization. Route the already resolved submission to the plugin backend at a narrow API boundary; preserve all method arguments/options and existing wrapper chains. Do not rebuild the batch seed loop independently.
- Freeze execution destination/settings for a native finite batch, even if the user changes selection while submission is in progress. Associate submissions explicitly; avoid an unscoped global “next request is farm” flag.
- Return the native prompt response shape with the canonical run ID and validation errors, so normal job tracking continues.
- Initial farm support is manual Run plus finite batch count. Existing local Run (On Change)/Run (Instant) retain their behavior. These browser-driven automatic modes must not silently create unlimited farm jobs; farm support for them is a later explicit policy.
- Handle workflows already containing a Submit to Deadline node without recursively submitting it again. Define and test a single farm submission path; preserve the existing local node-based path.
- Partial/node execution is outside the first whole-workflow release. Handle it explicitly instead of silently expanding a partial run to a full workflow. Also document Shift+Run/front behavior: Deadline ordering follows its scheduling settings, not a local queue insertion position.
- The inspected native job menu has no Retry entry. Prove a plugin-added job action in M0. Retry must use the stored snapshot through Python, not reopen a graph and run its widget callbacks again. Do not silently substitute a separate job panel.
- Prefer supported hooks; if a runtime DOM/UI adapter is necessary, isolate it, identify its version/element assumptions, preserve keyboard/focus behavior, remove observers/listeners on teardown, and detect unsupported frontend versions. Do not patch minified bundles, Vue closures, or stock files.

If the required dropdown/job-action/progress seams cannot be integrated reliably on this frontend, report the exact missing hook and demonstrated failure. Do not quietly change the product to a second Farm jobs UI. A different frontend version or upstream extension hook would be a separate decision.

## 7. Python control and status semantics

Plugin-specific routes, registered without the `/api` prefix because ComfyUI adds aliases:

- `GET /deadline/farm/capabilities`: protocol/version support, available transport, native adapter health, worker-session support, configuration readiness.
- `GET /deadline/farm/settings` and a validated update route: shared root, pool/group/priority, worker limits/allowlist, timeouts, endpoint/session policy. Reuse native settings UI where practical.
- `POST /deadline/farm/submit`: snapshot, idempotency key, batch association, captured options/settings; returns a native-compatible accepted run response.
- `POST /deadline/farm/<run_id>/retry`: creates a new run from the stored snapshot with an idempotent retry request key.
- Plugin cancellation/reconciliation functions used by native controls; add public routes only if a consumer needs them.

Use `DeadlineCommandHelper` initially, extending it for bounded query/control operations rather than introducing another service. Confirm command syntax and parse structured output on the installed Deadline version. A configured Deadline Web Service can be a later transport; do not add a required coordinator service.

Status policy:

- Deadline queued or retrying => native pending, with useful retry diagnostics.
- Active task/startup => native in progress; show measured progress or initialization rather than fabricated percentages.
- Completion requires a valid committed ComfyUI result. Reconcile Deadline/reporting acknowledgement races; never treat exit code alone as proof of outputs.
- Exhausted retries, preflight/execution failures, and invalid/missing outputs => native failed, with actionable node or infrastructure errors.
- User Stop => durable cancellation intent; suspend/stop Deadline scheduling using tested controls, then interrupt/dequeue only the corresponding worker prompt. Confirm actual stop before showing a terminal state. Prevent Deadline from simply retrying the interrupted prompt.
- Cancelling a terminal run is a no-op. Result publication and cancellation use the same lock so their ordering is deterministic.
- An externally suspended Deadline job is not automatically “cancelled.” Preserve its diagnosis and define its native representation without inventing a schema state.

The reusable endpoint path currently starts an own-prompt waiter. Killing that waiter alone is not proof that the ComfyUI prompt stopped. Test cancellation and Deadline Worker shutdown hooks explicitly. Never use an unqualified global interrupt on a borrowed endpoint.

Do not persist browser Comfy API auth tokens or credentials in the shared snapshot/metadata/logs. If a workflow needs authenticated services, define worker-side credential provisioning and report missing credentials. Validate job-info fields against newline injection and asset paths against traversal/allowed roots.

## 8. Implementation sequence and exit checks

Do each milestone as a small reviewable change. Record actual results and any changed decision in this document or a linked validation log. Do not call source inspection a live test.

### M0 — Prove the three high-risk seams

- [x] Install a temporary plugin-only native Jobs adapter with a deterministic fake farm provider.
- [x] Show local work plus two remote runs through pending/running/completed/failed/cancelled, complete workflow detail, outputs, Stop, and retry in the existing UI.
- [x] Prove per-run progress/previews with concurrent node IDs, active count/filtering/pagination, reconnect, and no local execution/canvas corruption.
- [x] Prove Run dropdown selection and submission routing while preserving normal widget seeds, keyboard invocation, batch count, and other API wrappers.
- [x] On a test Deadline Worker, measure cold startup, second-task reuse, cross-job session reuse, cancellation, sandbox teardown, and idle cleanup. Record PID/session identity, model reload/cache behavior for compatible workflows, elapsed time, and GPU memory behavior.
- [x] From two hosts using the proposed share, prove locking, fencing, atomic publication, crash recovery, and stale-writer rejection.

Exit: demonstrated native integration, viable coordinator-free session ownership, and safe durable state. Do not build the full feature on an unproven seam. A cold-run prototype may proceed for investigation, but warmth remains required for final completion.

### M1 — Durable payloads and one real farm run

- [x] Refactor staging/submission helpers for both the existing node and new Python entry point, preserving node behavior.
- [x] Implement versioned payload, receipt, control, attempt, and result records; idempotent submission/reconciliation; metadata discovery.
- [x] Submit one real one-task job, run existing worker preflight, publish validated result metadata, and open its readable workflow/output.
- [x] Shut down the submitting instance after acceptance and verify the run finishes. Restart and rediscover it from Deadline/shared metadata.
- [x] Test submit-response loss, crash before activation, crash after activation, and partial batch acceptance.

Exit: an accepted real run is independent of the submitter, with a recoverable native ID and immutable snapshot.

### M2 — Native queue/history, progress, results, and controls

- [x] Replace the fake provider with cached Python Deadline queries and shared result metadata.
- [x] Complete merged API filtering/pagination/detail/assets and required legacy controls.
- [x] Connect native Run and finite batches to real submission; add settings and capability diagnostics only, no second job list.
- [x] Connect native per-job Stop, batch Stop, error display, exact-snapshot Retry, previews, and view/download/Add to workflow.
- [ ] Test local and remote jobs together, workflow-tab changes, multiple browser tabs, backend restart, Deadline disconnect, and result-cache loss.

Exit: the existing Jobs UI handles the full farm lifecycle without compromising local runs.

### M3 — Persistent worker ownership and idle release

- [x] Implement the session lifecycle selected by M0 with explicit owned/borrowed modes and reservations.
- [x] Reuse relevant Distributed identity/registration/heartbeat logic without depending on the submitting master.
- [x] Preserve stable directories, per-attempt staging, GPU affinity, startup options, and environment compatibility checks.
- [x] Warm reuse across at least three independent jobs; drain on idle; recover from ComfyUI crash and Worker restart; prove no unrelated artist prompt/process is interrupted.
- [x] Document and test GPU release when other Deadline applications share the Worker.

Exit: warm session reuse and cleanup are supervised and work without the submitting computer.

### M4 — Failure recovery, packaging, and rollout

- [x] Kill a Worker mid-run; allow Deadline retry elsewhere. Publish a late result from the old attempt; only the current accepted result appears.
- [x] Exercise Stop-versus-completion, Stop-versus-retry, duplicate submission requests, duplicate callbacks, and result-commit/acknowledgement loss.
- [ ] Exercise missing nodes/models/inputs, browser-interactive nodes, missing output, task timeout, share outage, and incompatible worker session.
- [x] Test a five-run batch on two GPU Workers with the submitting computer off; no workflow splitting, no missing or duplicate visible results.
- [x] Deploy backend extension and Deadline helpers together; test from a fresh installed layout. Preserve existing node and Distributed regressions.
- [x] Document settings, storage/retention, worker setup, supported versions, limitations, transport/credential requirements, and rollback. Remove spike-only fixtures from production loading.

Implementation exit: the live durability, two-Worker batch, recovery, warm ownership, native controls/results, and real progress/preview gates passed. The unchecked validation matrix remains a rollout follow-up; see the linked record for the exact coverage and limits. Disable/rollback native adapters independently if compatibility changes.

## 9. Suggested code ownership

Use the fewest files that keep these responsibilities clear; names below are proposed, not a requirement to build a framework.

- `__init__.py`: register backend integration and `WEB_DIRECTORY`, preserve mappings.
- `deadline_submit.py`: shared submission/staging helpers and existing node compatibility.
- `farm_backend.py`: settings, submission, cached Deadline queries, reconciliation, and control endpoints.
- `native_jobs_adapter.py`: native API response shaping/routing and compatibility checks; no scheduling or worker dispatch.
- A small shared protocol/store helper: atomic records, locks, generations, metadata validation. Ensure the same implementation is available in the deployed Deadline plugin; test packaging/imports rather than assuming the render sandbox can import this custom-node checkout.
- `web/deadline_queue.js` and a small versioned compatibility module if needed: Run selection/routing and native progress/preview/retry integration. Keep settings separate from private UI seams.
- `plugins/ComfyUI/ComfyUI.py` plus a focused session helper if needed: executing assigned jobs, tracking own prompts, reporting/publishing results, and owned process lifetime.
- Distributed registration/monitor files: only the narrow reuse needed for session identity/liveness. Do not bring browser dispatch or workflow splitting into the new path.

Do not rewrite the existing 2,000+ line Deadline plugin wholesale just to support the new protocol. Preserve its existing tested execution and preflight paths. New behavior should be identified by an explicit protocol/mode marker in job metadata.

## 10. Acceptance and validation

Acceptance checklist (source and automated/live evidence recorded in the linked validation log; existing Distributed has regression coverage but has not been rerun as a full live Collector/Upscale job):

- [x] Only plugin repositories changed; ComfyUI core/frontend files unchanged.
- [x] One native Jobs/history UI; Deadline-specific UI contains settings/health rather than a duplicate job list.
- [ ] Normal local Run and existing node/Distributed workflows still work.
- [x] Resolved seeds/widget values match native submission; explicit Retry preserves the original snapshot.
- [x] All accepted runs, including waiting runs, complete with the submitter off.
- [x] Multiple Workers execute independent whole snapshots concurrently through Deadline assignments.
- [x] Native progress, previews, workflow metadata, outputs, errors, Stop, and retry refer to the correct run.
- [x] Restart/reconnect restores runs from durable state; local cached history is not the only record.
- [x] Worker/task retries cannot publish duplicate visible results; Stop cannot cause automatic re-execution.
- [x] Warm owned sessions persist across jobs and release idle GPU/process resources; borrowed artist sessions remain safe.
- [x] Unsupported versions/settings and disconnected services produce actionable diagnostics, not silent routing changes.

Meaningful automated tests: merged API filtering/pagination and local pass-through; real response-schema fixtures; submission acknowledgement loss; same-key/different-payload rejection; fencing/cancel/result races; safe result paths; and session ownership/drain rules. Existing stub tests remain useful but cannot replace multi-host SMB and live Deadline tests.

Run the existing suite against the checkout:

```powershell
& 'C:\AI\ComfyUI_windows_portable4\python_embeded\python.exe' -c "import sys, unittest; sys.path.insert(0, '.'); suite = unittest.defaultTestLoader.discover('tests'); result = unittest.TextTestRunner(verbosity=1).run(suite); sys.exit(not result.wasSuccessful())"
```

For browser verification, use T3's collaborative preview when available. Record screenshots and concise observed results for M0/M2. Keep any farm test jobs isolated to the chosen test Workers/pool and clean up their owned sessions/files through the implemented lifecycle.

## 11. First actions for the implementation agent

1. Read this plan and applicable repository instructions; inspect current diffs and versions.
2. Confirm `deadlinecommand`, repository aliases, live ComfyUI, shared root, and test Worker/GPU access. Discover existing configured defaults before requesting missing values.
3. Start M0 with the native adapter fixture and dropdown/retry/progress prototype. Do not start by building a Farm job panel or a standalone coordinator.
4. Run the persistent session and two-host storage spikes, record evidence, and settle their ownership/locking decisions before implementing the full durable path.
5. Continue M1–M4 in order. Keep the accepted product boundaries; report a demonstrated missing seam if a gate fails instead of silently changing the design.

Reference source paths outside this repository are for inspection only. No permission to modify ComfyUI core/frontend is implied by this handoff.
