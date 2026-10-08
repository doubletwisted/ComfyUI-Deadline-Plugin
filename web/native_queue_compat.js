// Frontend 1.53.10 only. No Vue component closures or installed bundles are
// modified. The private Pinia registry and DOM assumptions are kept here.
export function inspectNativeQueue(app, version) {
    const stores = app.extensionManager?._p?._s;
    const execution = stores?.get("execution");
    const previews = stores?.get("jobPreview");
    const queue = stores?.get("queue");
    const settings = app.extensionManager?.queueSettings;
    const problems = [];
    if (version !== "1.53.10") problems.push(`Frontend ${version} is unsupported; expected 1.53.10.`);
    if (!Array.isArray(app.queueItems) || !Number.isInteger(app.nextQueueRequestId)) problems.push("Native request association is unavailable.");
    if (!execution?.nodeProgressStatesByJob || typeof previews?.setPreviewUrl !== "function" || typeof queue?.update !== "function") {
        problems.push("Native per-job state stores are unavailable.");
    }
    if (!settings || !document.querySelector('[data-testid="queue-mode-menu-trigger"]')) problems.push("Native Run controls are unavailable.");
    return {problems, execution, previews, queue, settings};
}

export function captureDeadlineDestination(selected, mode, settings, readiness) {
    if (!selected || mode !== "disabled") return null;
    if (!readiness.ready) throw new Error(readiness.message || "Deadline is not ready. Open the Deadline sidebar.");
    return {settings: structuredClone(settings)};
}

export function installNativeControls({app, state, progress, getDestination, getReadiness, openSetup, selectDestination, retry, stop}) {
    let frame;
    let fallback;
    let disposed = false;
    let menuJobId;
    const positions = new Map();
    const cancelledLabels = new Map();
    const stopping = new Set();
    const queueButton = () => document.querySelector('[data-testid="queue-button"]');
    const trigger = () => document.querySelector('[data-testid="queue-mode-menu-trigger"]');
    const runMenu = () => {
        const id = trigger()?.id;
        return id ? document.querySelector(`[role="menu"][aria-labelledby="${CSS.escape(id)}"]`) : null;
    };
    const closeRunMenu = () => { trigger()?.click(); trigger()?.focus(); };
    const schedule = () => {
        if (disposed || frame || fallback) return;
        frame = requestAnimationFrame(render);
        // Webviews can suspend animation frames while their page remains
        // active. Menu actions still need to mount in that state.
        fallback = setTimeout(render, 100);
    };

    function render() {
        cancelAnimationFrame(frame);
        clearTimeout(fallback);
        frame = undefined;
        fallback = undefined;
        if (disposed) return;
        const farm = getDestination() !== null;
        const readiness = getReadiness();
        const button = queueButton();
        if (button) {
            const text = [...button.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
            if (farm && text && text.textContent.trim() !== "Run (Deadline)") text.textContent = " Run (Deadline)";
            if (farm && button.getAttribute("aria-label") !== "Run (Deadline)") button.setAttribute("aria-label", "Run (Deadline)");
            if (!farm && text?.textContent.includes("Deadline")) text.textContent = " Run";
            if (!farm && button.getAttribute("aria-label") === "Run (Deadline)") button.removeAttribute("aria-label");
        }
        const menu = runMenu();
        if (menu && !menu.querySelector("[data-deadline-mode]")) {
            const item = document.createElement("button");
            item.type = "button";
            item.role = "menuitem";
            item.tabIndex = -1;
            item.dataset.deadlineMode = "farm";
            item.className = menu.querySelector("button")?.className || "";
            item.textContent = "Run (Deadline)";
            item.title = "Whole workflows; finite native batch count. Deadline priority governs ordering, including Shift+Run.";
            item.addEventListener("click", event => {
                event.preventDefault();
                event.stopPropagation();
                if (!getReadiness().ready) return;
                state.settings.mode = "disabled";
                selectDestination(true);
                closeRunMenu();
                schedule();
            });
            menu.append(item);
        }
        if (menu) {
            const item = menu.querySelector("[data-deadline-mode]");
            item.disabled = !readiness.ready;
            item.title = readiness.ready ? "Whole workflows; finite native batch count. Deadline priority governs ordering, including Shift+Run." : readiness.message;
            item.setAttribute("aria-disabled", String(!readiness.ready));
            let setup = menu.querySelector("[data-deadline-setup]");
            if (!readiness.ready && openSetup && !setup) {
                setup = document.createElement("button");
                setup.type = "button"; setup.role = "menuitem"; setup.tabIndex = -1;
                setup.dataset.deadlineSetup = ""; setup.className = item.className;
                setup.textContent = "Open Deadline";
                setup.addEventListener("click", event => {
                    event.preventDefault(); event.stopPropagation(); closeRunMenu(); openSetup();
                });
                menu.append(setup);
            }
            if (readiness.ready) setup?.remove();
        }

        for (const row of document.querySelectorAll("[data-job-id]")) {
            const event = progress.runs.get(row.dataset.jobId);
            const active = event && ["pending", "in_progress"].includes(event.status);
            let stopButton = row.querySelector("[data-deadline-stop]");
            if (active && stop && !stopButton) {
                stopButton = document.createElement("button");
                stopButton.type = "button";
                stopButton.dataset.deadlineStop = row.dataset.jobId;
                stopButton.style.cssText = "position:relative;z-index:1;flex-shrink:0;font-size:11px;line-height:22px;padding:0 7px;background:var(--bg-color,#222);border:1px solid var(--border-color,#555);border-radius:4px;cursor:pointer";
                stopButton.addEventListener("click", async click => {
                    click.preventDefault(); click.stopPropagation();
                    const id = stopButton.dataset.deadlineStop;
                    if (stopping.has(id)) return;
                    stopping.add(id); schedule();
                    try { await stop(id); }
                    catch (error) { app.extensionManager.toast.addAlert(`Could not stop Deadline run: ${error.message}`); }
                    finally { stopping.delete(id); schedule(); }
                });
                (row.firstElementChild || row).append(stopButton);
            }
            if (!active) stopButton?.remove();
            else if (stopButton) {
                stopButton.dataset.deadlineStop = row.dataset.jobId;
                const requested = event.cancel_requested || stopping.has(row.dataset.jobId);
                stopButton.disabled = !!requested;
                const label = requested ? "Stopping…" : "Stop";
                if (stopButton.textContent !== label) stopButton.textContent = label;
                stopButton.setAttribute("aria-label", requested ? "Stopping Deadline run" : "Stop Deadline run");
                stopButton.title = requested ? "Waiting for the Deadline task to stop" : "Stop this run on Deadline";
            }
            // This frontend groups cancelled runs in its Failed tab. Keep its
            // API status intact and distinguish farm Stop in the native row.
            for (const label of row.querySelectorAll('span.truncate[title]')) {
                if (event?.status === "cancelled" && label.getAttribute("title") === "Failed") {
                    cancelledLabels.set(label, {text: label.textContent, title: label.title, run: row.dataset.jobId});
                    label.textContent = "Cancelled";
                    label.title = "Cancelled";
                } else if (cancelledLabels.has(label) && (event?.status !== "cancelled" || cancelledLabels.get(label).run !== row.dataset.jobId)) {
                    const prior = cancelledLabels.get(label);
                    if (label.title === "Cancelled") { label.textContent = prior.text; label.title = prior.title; }
                    cancelledLabels.delete(label);
                }
            }
            let badge = row.querySelector("[data-deadline-progress]");
            if (!event || event.status !== "in_progress") { badge?.remove(); continue; }
            if (!badge) {
                badge = document.createElement("span");
                badge.dataset.deadlineProgress = row.dataset.jobId;
                badge.style.cssText = "position:absolute;right:44px;bottom:0;font-size:10px;line-height:12px;background:var(--bg-color,#222);padding:0 3px;pointer-events:none";
                if (!positions.has(row)) positions.set(row, row.style.position);
                row.style.position = "relative";
                row.append(badge);
            }
            const nodes = Object.values(event.nodes || {});
            const max = nodes.reduce((sum, node) => sum + node.max, 0);
            const value = nodes.reduce((sum, node) => sum + node.value, 0);
            const label = event.cancel_requested ? "Deadline stopping" : max > 0 ? `Deadline ${Math.round(100 * value / max)}%` : "Deadline initializing";
            if (badge.textContent !== label) badge.textContent = label;
        }
        for (const row of positions.keys()) if (!row.isConnected) positions.delete(row);
        for (const label of cancelledLabels.keys()) if (!label.isConnected) cancelledLabels.delete(label);

        // Copy-ID is a stable, inspected native context-menu action on this
        // supported English frontend. Unsupported menu layouts get no injection.
        for (const anchor of document.querySelectorAll('button[aria-label="Copy job ID"]')) {
            const menu = anchor.parentElement;
            const event = progress.runs.get(menuJobId);
            const terminal = event && ["completed", "failed", "cancelled"].includes(event.status);
            let action = menu.querySelector("[data-deadline-retry]");
            if (!terminal) { action?.remove(); continue; }
            if (action?.dataset.deadlineRetry === menuJobId) continue;
            action?.remove();
            action = document.createElement("button");
            action.type = "button";
            action.dataset.deadlineRetry = menuJobId;
            action.setAttribute("aria-label", "Retry on Deadline");
            action.className = anchor.className;
            action.textContent = "Retry on Deadline";
            const id = menuJobId;
            action.addEventListener("click", async event => {
                event.stopPropagation();
                action.disabled = true;
                try { await retry(id); } finally { action.disabled = false; }
            });
            menu.append(action);
        }
    }

    function captureClick(event) {
        const row = event.target.closest?.("[data-job-id]");
        if (row) menuJobId = row.dataset.jobId;
        const menu = runMenu();
        if (menu?.contains(event.target) && !event.target.closest("[data-deadline-mode],[data-deadline-setup]")) {
            selectDestination(false);
            const button = queueButton();
            const text = [...(button?.childNodes || [])].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
            if (text?.textContent.includes("Deadline")) text.textContent = " Run";
            button?.removeAttribute("aria-label");
            schedule();
        }
    }
    function keydown(event) {
        const menu = runMenu();
        if (!menu?.contains(event.target)) return;
        const items = [...menu.querySelectorAll('[role="menuitem"]')].filter(item => !item.disabled);
        const index = items.indexOf(event.target);
        const extensionItem = item => item?.matches("[data-deadline-mode],[data-deadline-setup]");
        if (index >= 0 && ["ArrowDown", "ArrowUp"].includes(event.key)) {
            const next = items[(index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
            if (extensionItem(event.target) || extensionItem(next)) {
                event.preventDefault(); event.stopImmediatePropagation(); next.focus();
            }
        } else if (extensionItem(event.target) && ["Enter", " "].includes(event.key)) {
            event.preventDefault(); event.stopImmediatePropagation(); event.target.click();
        }
    }

    const observer = new MutationObserver(schedule);
    observer.observe(document.body, {childList: true, subtree: true, characterData: true});
    document.addEventListener("click", captureClick, true);
    document.addEventListener("contextmenu", captureClick, true);
    document.addEventListener("keydown", keydown, true);
    schedule();
    return {
        render: schedule,
        dispose() {
            disposed = true;
            observer.disconnect();
            cancelAnimationFrame(frame);
            clearTimeout(fallback);
            document.removeEventListener("click", captureClick, true);
            document.removeEventListener("contextmenu", captureClick, true);
            document.removeEventListener("keydown", keydown, true);
            for (const element of document.querySelectorAll("[data-deadline-mode],[data-deadline-setup],[data-deadline-progress],[data-deadline-retry],[data-deadline-stop]")) element.remove();
            for (const [row, position] of positions) row.style.position = position;
            for (const [label, prior] of cancelledLabels) {
                if (label.title === "Cancelled") { label.textContent = prior.text; label.title = prior.title; }
            }
            const button = queueButton();
            const text = [...(button?.childNodes || [])].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.includes("Deadline"));
            if (text) text.textContent = " Run";
            button?.removeAttribute("aria-label");
        }
    };
}
