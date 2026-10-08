const MARKER = "__deadline_native_queue";
const DISTRIBUTED_NODES = new Set(["DistributedCollector", "DeadlineDistributedCollector", "DistributedVideoCollector",
    "DeadlineDistributedVideoCollector", "UltimateSDUpscaleDistributed", "DeadlineUltimateSDUpscaleDistributed"]);

function farmError(message) {
    return new Response(JSON.stringify({error: {type: "deadline_submission_error", message, details: ""}, node_errors: {}}),
        {status: 400, headers: {"Content-Type": "application/json"}});
}

// These internal boundaries are verified only for frontend 1.53.10. Route
// tokens travel with a particular serialized workflow, never the next request.
export function installRouting(app, api, captureDestination) {
    const originalQueue = app.queuePrompt;
    const originalGraph = app.graphToPrompt;
    const originalPop = app.queueItems.pop;
    const originalFetch = api.fetchApi;
    const batches = new Map();
    const requests = new Map();
    let processingBatch;

    const queue = function (...args) {
        const destination = captureDestination();
        if (destination) {
            const count = args[1] ?? 1;
            if (!Number.isInteger(count) || count < 1 || count > 10000) throw new Error("Run (Deadline) requires a finite batch count between 1 and 10000.");
            const options = args[2];
            const targets = Array.isArray(options) ? options : options?.queueNodeIds;
            if (targets?.length) throw new Error("Run (Deadline) supports whole workflows only.");
        }
        const requestId = this.nextQueueRequestId;
        batches.set(requestId, destination ? structuredClone({...destination, batch_id: crypto.randomUUID()}) : null);
        return originalQueue.apply(this, args);
    };
    const pop = function (...args) {
        const item = originalPop.apply(this, args);
        processingBatch = item ? batches.get(item.requestId) : null;
        if (item) batches.delete(item.requestId);
        return item;
    };
    const graph = async function (...args) {
        const batch = processingBatch;
        const data = await originalGraph.apply(this, args);
        if (!batch || !app.processingQueue) return data;
        if (Object.values(data.output).some(node => DISTRIBUTED_NODES.has(node.class_type))) {
            throw new Error("Use local Run for workflows containing Distributed nodes. Run (Deadline) queues whole workflows.");
        }
        const token = crypto.randomUUID();
        requests.set(token, {...batch, submission_key: crypto.randomUUID(), prepared_at: Date.now()});
        // Keep bounded retry contexts, including responses lost in another
        // extension's transport wrapper. They carry no prompt or credentials.
        for (const [key, context] of requests) if (Date.now() - context.prepared_at > 300000) requests.delete(key);
        return {...data, workflow: {...data.workflow, [MARKER]: token}};
    };
    const fetch = async function (route, options, ...args) {
        if (route !== "/prompt" || options?.method !== "POST") {
            return originalFetch.call(this, route, options, ...args);
        }
        const body = JSON.parse(options.body);
        const workflow = body.extra_data?.extra_pnginfo?.workflow;
        const token = workflow?.[MARKER];
        if (!token) return originalFetch.call(this, route, options, ...args);
        const context = requests.get(token);
        if (!context) return farmError("Deadline request context expired; the workflow was not queued locally.");
        if (body.partial_execution_targets?.length) return farmError("Run (Deadline) supports whole workflows only.");
        if (Object.values(body.prompt).some(node => ["DeadlineSubmit", "SaveAndSubmitNode"].includes(node.class_type))) {
            return farmError("Choose local Run for workflows containing Submit to Deadline, or remove that node for Run (Deadline).");
        }
        delete workflow[MARKER];
        // Browser credentials are never sent to shared farm storage.
        delete body.extra_data.auth_token_comfy_org;
        delete body.extra_data.api_key_comfy_org;
        delete body.front;
        delete body.number;
        const response = await originalFetch.call(this, "/deadline/farm/submit", {
            ...options, body: JSON.stringify({...body, settings: context.settings,
                batch_id: context.batch_id, submission_key: context.submission_key})
        }, ...args);
        api.dispatchCustomEvent("deadline:queue-changed", {});
        return response;
    };

    app.queuePrompt = queue;
    app.queueItems.pop = pop;
    app.graphToPrompt = graph;
    api.fetchApi = fetch;
    return () => {
        if (app.queuePrompt === queue) app.queuePrompt = originalQueue;
        if (app.queueItems.pop === pop) app.queueItems.pop = originalPop;
        if (app.graphToPrompt === graph) app.graphToPrompt = originalGraph;
        if (api.fetchApi === fetch) api.fetchApi = originalFetch;
        requests.clear();
        batches.clear();
    };
}

export function installActivePagination(api, maximum = 10000) {
    const previous = api.fetchApi;
    const fetch = async function (route, ...args) {
        const response = await previous.call(this, route, ...args);
        const url = new URL(route, "http://localhost");
        const statuses = new Set((url.searchParams.get("status") || "").split(","));
        if (url.pathname !== "/jobs" || statuses.size !== 2 || !statuses.has("pending") ||
            !statuses.has("in_progress") || url.searchParams.get("offset") !== "0" || !response.ok) return response;
        const data = await response.clone().json();
        while (data.pagination.has_more) {
            if (data.jobs.length >= maximum) throw new Error(`Native queue exceeds the ${maximum} supported active runs; list is incomplete.`);
            url.searchParams.set("offset", String(data.jobs.length));
            const pageResponse = await previous.call(this, url.pathname + url.search, ...args);
            if (!pageResponse.ok) throw new Error("Could not load the complete native active queue.");
            const page = await pageResponse.json();
            if (!page.jobs.length) throw new Error("Active queue pagination made no progress.");
            data.jobs.push(...page.jobs);
            data.pagination = page.pagination;
        }
        data.pagination.offset = 0;
        data.pagination.limit = data.jobs.length;
        data.pagination.total = data.jobs.length;
        return new Response(JSON.stringify(data), {status: response.status, headers: response.headers});
    };
    api.fetchApi = fetch;
    return () => { if (api.fetchApi === fetch) api.fetchApi = previous; };
}

export class FarmProgress {
    constructor() { this.runs = new Map(); }
    apply(event) {
        const previous = this.runs.get(event.run_id);
        if (previous && (event.generation < previous.generation ||
            (event.generation === previous.generation && event.sequence <= previous.sequence))) return false;
        this.runs.set(event.run_id, event);
        return true;
    }
    restore(events) {
        const present = new Set(events.map(event => event.run_id));
        for (const id of this.runs.keys()) if (!present.has(id)) this.runs.delete(id);
        for (const event of events) this.apply(event);
    }
}
