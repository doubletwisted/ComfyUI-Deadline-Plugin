# Run workflows on Deadline

Open **Deadline** in the left sidebar. Choose a pool, group and priority; changes save automatically. **More options** contains task timeout and an optional **Only these Workers** restriction. Leave the restriction empty to let Deadline choose.

Click **Run on Deadline**, or select **Run (Deadline)** in the native Run dropdown. Native batch count and seed controls work as usual. Each workflow becomes a Deadline job, and farm results appear in the existing **Jobs** panel. Accepted runs continue after you close ComfyUI.

Use **Stop** on a queued or running farm job. **Stopping…** remains visible until cancellation is confirmed. Deleted Deadline jobs leave the active queue; their snapshots remain available for history and retry. A committed result stays completed even if its Deadline job is subsequently removed.

Warm reuse is an optional farm configuration, not an artist reservation. If the queue reports missing storage or render helpers, see the [administrator guide](DEADLINE_NATIVE_QUEUE_ADMIN.md). Existing Submit to Deadline and DeadlineSeed nodes remain available.
