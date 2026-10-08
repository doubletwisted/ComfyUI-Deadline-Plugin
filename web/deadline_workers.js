const key = value => String(value || "").trim().toLowerCase();
const matches = (names, selected) => !key(selected) || key(selected) === "none" || names.some(name => key(name) === key(selected));

export function evaluateWorkers(settings, discovery, {search = ""} = {}) {
    const known = new Map();
    for (const worker of discovery.workers || []) {
        if (!known.has(key(worker.name))) known.set(key(worker.name), worker);
    }
    const selectedNames = new Map();
    for (const name of String(settings.workers || "").split(",")) {
        if (key(name) && !selectedNames.has(key(name))) selectedNames.set(key(name), name.trim());
    }

    function evaluate(worker, missing = false) {
        const pools = worker.pools || [], groups = worker.groups || [];
        const poolMatches = matches(pools, settings.pool), groupMatches = matches(groups, settings.group);
        const enabled = worker.enabled === true;
        const reasonCodes = [], reasons = [];
        const reason = (code, message) => {reasonCodes.push(code); reasons.push(message);};
        if (missing) reason("missing", "Worker is no longer in the repository");
        else {
            if (!enabled) reason("disabled", "Worker is disabled in Deadline");
            const mode = key(worker.scheduling_mode === undefined ? "AllJobs" : worker.scheduling_mode);
            if (mode === "userjobs") {
                const users = worker.user_job_names || [];
                if (!users.some(user => key(user) === key(discovery.deadline_user || settings.scope)))
                    reason("user", `Does not accept Deadline user ${discovery.deadline_user || settings.scope || "(empty)"}`);
            } else if (mode === "machinejobs") {
                if (!key(worker.machine_user)) reason("user", "Deadline machine user could not be verified");
                else if (key(worker.machine_user) !== key(discovery.deadline_user || settings.scope)) reason("user", `Accepts only Deadline machine user ${worker.machine_user}`);
            } else if (mode !== "alljobs") {
                reason("scheduling", `Deadline scheduling mode ${worker.scheduling_mode} could not be verified`);
            }
            if (!poolMatches) reason("pool", `Not in pool ${settings.pool}`);
            if (!groupMatches) reason("group", `Not in group ${settings.group}`);
            if (worker.session_error) reason("session_error", "ComfyUI session configuration is invalid");
        }
        const status = key(worker.status);
        const availability = missing ? "missing" : !enabled ? "disabled" : status === "idle" ? "idle" :
            status === "rendering" ? "busy" : status === "offline" ? "offline" : "unknown";
        const eligible = reasons.length === 0;
        return {...worker, pools, groups, selected: selectedNames.has(key(worker.name)), missing,
            matchesFilters: poolMatches && groupMatches, eligible,
            available: eligible && availability === "idle", availability, reasons, reasonCodes};
    }

    const workers = [...known.values()].map(worker => evaluate(worker));
    const byName = new Map(workers.map(worker => [key(worker.name), worker]));
    const selected = [...selectedNames].map(([name, original]) => byName.get(name) || evaluate({name: original, status: "Missing"}, true));
    const term = key(search);
    const candidates = workers.filter(worker => !worker.selected &&
        worker.matchesFilters &&
        (!term || `${worker.name} ${worker.host || ""} ${worker.gpu_model || ""}`.toLowerCase().includes(term)));
    const order = (a, b) => Number(b.available) - Number(a.available) || Number(b.eligible) - Number(a.eligible) || a.name.localeCompare(b.name);
    selected.sort(order); candidates.sort(order);

    return {selected, candidates, counts: {
        farm: workers.length,
        matching: workers.filter(worker => worker.matchesFilters).length,
        eligible: workers.filter(worker => worker.eligible).length,
        available: workers.filter(worker => worker.available).length,
        selected: selected.length,
        selectedEligible: selected.filter(worker => worker.eligible).length,
        selectedAvailable: selected.filter(worker => worker.available).length,
        missing: selected.filter(worker => worker.missing).length,
        visible: selected.length + candidates.length,
        hidden: workers.length - selected.filter(worker => !worker.missing).length - candidates.length
    }};
}
