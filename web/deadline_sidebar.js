import {DeadlineSettings} from "./deadline_settings.js";
import {evaluateWorkers} from "./deadline_workers.js";

export async function fetchFarmDiscovery(api, force, signal) {
    const response = await api.fetchApi(`/deadline/farm/discovery${force ? "?refresh=1" : ""}`, {signal});
    if (response.status === 404) {
        const error = new Error("Restart ComfyUI to load the updated Deadline backend.");
        error.restartRequired = true;
        throw error;
    }
    if (!response.ok) throw new Error(`Farm discovery unavailable (${response.status})`);
    return response.json();
}

export function farmConnectionStatus(discovery) {
    if (discovery.restart_required) return "Restart required";
    if (discovery.connected === null) return "Connecting";
    return discovery.connected ? "Connected" : "Disconnected";
}

export function sidebarReadiness(settings, discovery, workers, server, dirty) {
    if (discovery.restart_required) return {title:"Restart required", message:discovery.message, action:"refresh"};
    if (!discovery.connected) return {title:"Farm unavailable", message:discovery.message || "Connecting to Deadline…", action:"refresh"};
    if (dirty) return {title:"Saving changes", message:"Your next run will use the updated destination."};
    if (!settings.root || !settings.enabled) return {title:"Farm queue is not configured", message:"Ask your farm administrator to configure shared storage and deploy the native queue plugin.", action:"guide"};
    if (!discovery.plugin_available) return {title:"Farm plugin needs updating", message:"The native queue helpers need to be deployed by your farm administrator.", action:"guide"};
    const invalid = workers.selected.filter(worker => !worker.eligible);
    if (invalid.length) return {title:"Worker restriction needs attention", message:invalid.map(worker => `${worker.name}: ${worker.reasons.join(", ")}`).join("; "), action:"workers"};
    if (!server?.ready) return {title:"Farm queue unavailable", message:server?.message || "Checking shared storage…", action:["plugin","storage","session_configuration"].includes(server?.code) ? "guide" : "refresh"};
    return {title:"Ready", message:"Deadline schedules each workflow. Batch count and seeds follow native Run.", ready:true};
}

export function installDeadlineSidebar(app, api, capabilities, onSaved) {
    if (!document.querySelector("link[data-deadline-sidebar]")) {
        const css = document.createElement("link");
        css.rel = "stylesheet";
        css.href = new URL("./deadline_sidebar.css", import.meta.url).href;
        css.dataset.deadlineSidebar = "";
        document.head.append(css);
    }
    let panel, controller, refreshTimer, saveTimer, run;
    let refreshing = false, generation = 0, search = "";
    let discovery = {connected:null, workers:[], pools:[], groups:[]};
    let readiness;
    let problems = capabilities.mode === "unsupported" ? [capabilities.message] : [];
    const model = new DeadlineSettings(api, onSaved, update);
    const $ = selector => panel?.querySelector(selector);
    const selection = () => evaluateWorkers(model.draft, discovery, {search});

    function nativeReadiness() {
        if (problems.length) return {ready:false, message:problems.join(" ")};
        if (model.saving || model.dirty) return {ready:false, message:"Wait for Deadline settings to finish saving."};
        if (model.conflict || model.error) return {ready:false, message:model.error || "Settings changed in another browser. Reload settings."};
        if (!model.saved?.enabled) return {ready:false, message:"The farm queue needs administrator configuration."};
        return readiness || {ready:false, message:"Checking the farm queue."};
    }

    function setText(selector, value) {
        const element = $(selector);
        if (element) element.textContent = value;
    }

    function setOptions(key, names) {
        const select = $(`[name="${key}"]`);
        const values = [...new Set(["none", ...names, model.draft[key]].filter(Boolean))];
        if (select.dataset.options !== JSON.stringify(values)) {
            select.replaceChildren(...values.map(value => {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = value === "none" ? `Any ${key}` : value;
                return option;
            }));
            select.dataset.options = JSON.stringify(values);
        }
        select.value = model.draft[key] || "none";
    }

    function edit(key, value) {
        model.edit(key, value);
        clearTimeout(saveTimer);
        saveTimer = setTimeout(save, 400);
    }

    async function save() {
        if (await model.save()) await refresh(true);
        update();
    }

    function workerRow(worker) {
        const label = document.createElement("label");
        label.className = "dl-worker";
        label.dataset.worker = worker.name;
        label.dataset.invalid = String(!worker.eligible);
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = worker.selected;
        checkbox.disabled = model.saving || !worker.eligible && !worker.selected;
        checkbox.setAttribute("aria-label", `Use Worker ${worker.name}`);
        checkbox.addEventListener("change", () => {
            const names = (model.draft.workers || "").split(",").filter(name => name && name.toLowerCase() !== worker.name.toLowerCase());
            if (checkbox.checked) names.push(worker.name);
            edit("workers", names.join(","));
        });
        const body = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = worker.name;
        const detail = document.createElement("small");
        detail.textContent = worker.reasons.join(" · ") || [worker.host !== worker.name ? worker.host : "", worker.gpu_model].filter(Boolean).join(" · ");
        body.append(name);
        if (detail.textContent) body.append(detail);
        const status = document.createElement("span");
        status.className = "dl-worker-status";
        status.dataset.state = worker.availability;
        status.textContent = worker.availability === "disabled" ? "Disabled" : worker.status || "Unknown";
        label.append(checkbox, body, status);
        return label;
    }

    function update() {
        if (!panel) return;
        const workers = selection();
        const state = sidebarReadiness(model.draft, discovery, workers, readiness, model.dirty);
        setText(".dl-connection-label", farmConnectionStatus(discovery));
        $(".dl-connection").dataset.connected = String(Boolean(discovery.connected));
        $(".dl-connection").title = discovery.repository || discovery.message || "";
        $(".dl-plugin-override").hidden = !model.saved?.plugin_directory;
        $(".dl-plugin-override").title = model.saved?.plugin_directory || "";
        setOptions("pool", discovery.pools);
        setOptions("group", discovery.groups);
        for (const input of panel.querySelectorAll("[name]")) {
            input.disabled = !model.saved || model.saving;
            if (input !== document.activeElement) input.value = model.draft[input.name] ?? "";
        }
        const destination = evaluateWorkers(model.draft, discovery);
        const targets = model.draft.workers ? destination.selected : destination.candidates.filter(worker => worker.eligible);
        const idle = targets.filter(worker => worker.available).length;
        const busy = targets.filter(worker => worker.eligible && worker.availability === "busy").length;
        setText(".dl-destination-summary", `${targets.length} Worker${targets.length === 1 ? "" : "s"} · ${idle} idle · ${busy} rendering`);
        setText(".dl-destination-note", model.draft.workers ? `Only ${targets.slice(0,2).map(worker => worker.name).join(", ")}${targets.length > 2 ? ` + ${targets.length - 2} more` : ""}` : "Deadline chooses an available Worker.");
        $(".dl-notice").hidden = Boolean(state.ready && !problems.length);
        setText(".dl-notice-title", problems.length ? "Native Run unavailable" : state.title);
        setText(".dl-notice-message", problems.length ? problems.join(" ") : state.message);
        $(".dl-guide-link").hidden = state.action !== "guide";
        $(".dl-review-workers").hidden = state.action !== "workers";
        setText(".dl-restriction-summary", model.draft.workers ? `${workers.selected.length} selected` : "Optional");
        const list = $(".dl-worker-list");
        const fingerprint = JSON.stringify([workers.selected, workers.candidates, model.saving]);
        if (list.dataset.fingerprint !== fingerprint) {
            const focused = document.activeElement?.closest(".dl-worker")?.dataset.worker;
            list.replaceChildren(...[...workers.selected, ...workers.candidates].map(workerRow));
            list.dataset.fingerprint = fingerprint;
            if (focused) [...list.children].find(row => row.dataset.worker === focused)?.querySelector("input")?.focus({preventScroll:true});
        }
        $(".dl-no-workers").hidden = Boolean(workers.selected.length || workers.candidates.length);
        $(".dl-clear-workers").hidden = !model.draft.workers;
        $(".dl-clear-workers").disabled = model.saving;
        setText(".dl-save-state", model.saving || model.dirty ? "Saving…" : "");
        const error = model.conflict ? "Settings changed in another browser. Reload before editing." : model.error;
        setText(".dl-error", error);
        $(".dl-error").hidden = !error;
        $(".dl-reload").hidden = !model.conflict;
        $(".dl-retry-save").hidden = !model.error || model.conflict;
        $("[data-action=run]").disabled = !run || !state.ready || !nativeReadiness().ready;
        $("[data-action=run]").title = state.ready ? "Uses native batch count and seeds" : state.message;
        $("[data-action=refresh]").disabled = refreshing;
    }

    async function refresh(force = false) {
        if (refreshing || !panel) return;
        clearTimeout(refreshTimer);
        refreshing = true;
        update();
        const current = generation;
        const signal = controller.signal;
        try {
            const results = await Promise.allSettled([
                model.load(signal), fetchFarmDiscovery(api, force, signal),
                api.fetchApi("/deadline/farm/readiness", {signal}).then(async response => {
                    if (!response.ok) throw new Error("Could not check the farm queue");
                    return response.json();
                })
            ]);
            if (current !== generation) return;
            if (results[0].status === "rejected" && results[0].reason.name !== "AbortError") model.error = results[0].reason.message;
            discovery = results[1].status === "fulfilled" ? results[1].value : {...discovery, connected:false, message:results[1].reason.message, restart_required:Boolean(results[1].reason.restartRequired)};
            readiness = results[2].status === "fulfilled" ? results[2].value : {ready:false, message:results[2].reason.message};
        } finally {
            if (current === generation) {
                refreshing = false;
                update();
                if (panel) refreshTimer = setTimeout(refresh, 15000);
            }
        }
    }

    function destroy() {
        generation++;
        controller?.abort();
        clearTimeout(refreshTimer);
        panel = null;
        refreshing = false;
    }

    app.extensionManager.registerSidebarTab({
        id:"deadline", title:"Deadline", tooltip:"Deadline render farm", icon:"pi pi-send", type:"custom",
        render(container) {
            destroy();
            controller = new AbortController();
            container.style.cssText = "height:100%;min-height:0;overflow:hidden";
            panel = document.createElement("div");
            panel.className = "deadline-sidebar";
            panel.innerHTML = `
                <header class="dl-header"><div><h2>Deadline</h2><div class="dl-connection"><span class="dl-connection-dot"></span><span class="dl-connection-label">Connecting</span></div></div><button type="button" data-action="refresh" aria-label="Refresh farm" title="Refresh farm"><i class="pi pi-refresh" aria-hidden="true"></i></button></header>
                <div class="dl-content">
                    <section class="dl-notice" role="status"><strong class="dl-notice-title"></strong><p class="dl-notice-message"></p><a class="dl-guide-link" target="_blank" rel="noopener">Administrator guide <i class="pi pi-external-link" aria-hidden="true"></i></a><button type="button" class="dl-review-workers" hidden>Review restriction</button></section>
                    <section><h3>Destination</h3><div class="dl-grid"><label>Pool<select name="pool"></select></label><label>Group<select name="group"></select></label></div><p class="dl-destination-summary"></p><small class="dl-destination-note"></small><small class="dl-plugin-override" hidden>Custom render plugin active</small></section>
                    <section class="dl-priority"><label>Priority<input name="priority" type="number" min="0" max="100"></label></section>
                    <details class="dl-more"><summary>More options</summary><label>Task timeout (minutes)<input name="task_timeout_minutes" type="number" min="1" max="1440"></label>
                        <details class="dl-restriction"><summary>Only these Workers <span class="dl-restriction-summary"></span></summary><small>Leave empty to let Deadline choose.</small><input class="dl-search" type="search" placeholder="Search Workers" aria-label="Search Workers"><button type="button" class="dl-clear-workers">Clear restriction</button><div class="dl-worker-list"></div><p class="dl-no-workers" hidden>No Workers match this destination.</p></details>
                    </details>
                </div>
                <footer><p class="dl-error" role="alert" hidden></p><button class="dl-reload" type="button" hidden>Reload settings</button><button class="dl-retry-save" type="button" hidden>Retry saving</button><small class="dl-save-state" role="status"></small><div class="dl-buttons"><button type="button" data-action="jobs">Jobs</button><button type="button" data-action="run" class="dl-run"><i class="pi pi-play" aria-hidden="true"></i> Run on Deadline</button></div></footer>`;
            container.replaceChildren(panel);
            $(".dl-guide-link").href = api.apiURL("/deadline/farm/setup-guide");
            for (const input of panel.querySelectorAll("[name]")) input.addEventListener("change", () => edit(input.name, input.type === "number" ? Number(input.value) : input.value));
            $(".dl-search").value = search;
            $(".dl-search").addEventListener("input", event => {search = event.target.value; update();});
            $(".dl-clear-workers").addEventListener("click", () => edit("workers", ""));
            $(".dl-review-workers").addEventListener("click", () => {$(".dl-more").open = true; $(".dl-restriction").open = true; $(".dl-search").focus();});
            $(".dl-reload").addEventListener("click", () => {model.revert(); refresh(true);});
            $(".dl-retry-save").addEventListener("click", save);
            $("[data-action=refresh]").addEventListener("click", () => {clearTimeout(refreshTimer); refresh(true);});
            $("[data-action=run]").addEventListener("click", () => {if (nativeReadiness().ready) run?.();});
            $("[data-action=jobs]").addEventListener("click", () => {
                const button = document.querySelector('[data-testid="queue-overlay-toggle"]');
                if (button?.getAttribute("aria-pressed") !== "true") button?.click();
            });
            update();
            refresh();
        }, destroy
    });
    const dispose = () => {
        destroy();
        clearTimeout(saveTimer);
        app.extensionManager.unregisterSidebarTab("deadline");
        window.removeEventListener("pagehide", dispose);
    };
    window.addEventListener("pagehide", dispose);
    return {model, dispose, getReadiness:nativeReadiness,
        openSetup() {if (!panel?.isConnected) app.extensionManager.command.execute("Workspace.ToggleSidebarTab.deadline");},
        updateHealth(_health, checked) {if (checked) readiness = checked; update();},
        setRun(callback, errors = []) {run = callback; problems = errors; update();}
    };
}
