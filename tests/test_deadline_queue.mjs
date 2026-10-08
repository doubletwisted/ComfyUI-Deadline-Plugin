import {test} from "node:test";
import assert from "node:assert/strict";
import {FarmProgress, installActivePagination, installRouting} from "../web/deadline_routing.js";
import {captureDeadlineDestination} from "../web/native_queue_compat.js";

function fixture() {
    const calls = [];
    const args = [];
    const api = {
        dispatchCustomEvent() {},
        async fetchApi(route, options) {
            calls.push({route, body: JSON.parse(options.body)});
            return new Response(JSON.stringify({prompt_id: crypto.randomUUID(), node_errors: {}}));
        },
        async queuePrompt(number, data, options) {
            args.push([number, options]);
            const response = await this.fetchApi("/prompt", {method: "POST", body: JSON.stringify({
                prompt: data.output, extra_data: {auth_token_comfy_org: "private", api_key_comfy_org: "private",
                    extra_pnginfo: {workflow: data.workflow}}, partial_execution_targets: options?.partialExecutionTargets,
                front: number === -1
            })});
            return await response.json();
        }
    };
    let seed = 40;
    const app = {
        nextQueueRequestId: 1, queueItems: [], processingQueue: false,
        async graphToPrompt() { return {output: {"1": {class_type: "KSampler", inputs: {seed: seed++}}}, workflow: {id: "workflow"}}; },
        async queuePrompt(number, count = 1, options) {
            this.queueItems.push({number, count, options, requestId: this.nextQueueRequestId++});
            if (this.processingQueue) return false;
            this.processingQueue = true;
            try {
                while (this.queueItems.length) {
                    const item = this.queueItems.pop();
                    for (let i = 0; i < item.count; i++) {
                        const data = await this.graphToPrompt();
                        await api.queuePrompt(item.number, data, item.options);
                    }
                }
            } finally { this.processingQueue = false; }
            return true;
        }
    };
    return {api, app, calls, args};
}

test("finite native batch freezes destination, settings and seeds and preserves wrappers/options", async () => {
    const f = fixture();
    let destination = {settings: {pool: "test", priority: 50}};
    const restore = installRouting(f.app, f.api, () => destination);
    const queue = f.api.queuePrompt;
    let wrapperCalls = 0;
    f.api.queuePrompt = function (...args) { wrapperCalls++; return queue.apply(this, args); };
    const original = f.api.fetchApi;
    f.api.fetchApi = function (...args) {
        if (destination) destination.settings.pool = "changed";
        destination = null;
        return original.apply(this, args);
    };
    const options = {previewMethod: "latent2rgb", arbitraryOption: true};
    await f.app.queuePrompt(-1, 3, options);
    assert.equal(wrapperCalls, 3);
    assert.deepEqual(f.calls.map(call => call.route), Array(3).fill("/deadline/farm/submit"));
    assert.deepEqual(f.calls.map(call => call.body.prompt["1"].inputs.seed), [40, 41, 42]);
    assert.equal(new Set(f.calls.map(call => call.body.batch_id)).size, 1);
    assert.equal(new Set(f.calls.map(call => call.body.submission_key)).size, 3);
    for (const call of f.calls) {
        assert.equal(call.body.settings.pool, "test");
        assert.equal(call.body.front, undefined);
        assert.equal(call.body.extra_data.auth_token_comfy_org, undefined);
        assert.equal(call.body.extra_data.api_key_comfy_org, undefined);
        assert.equal(call.body.extra_data.extra_pnginfo.workflow.__deadline_native_queue, undefined);
    }
    assert.ok(f.args.every(([number, opts]) => number === -1 && opts === options));
    restore();
});

test("unready selected farm stops before widgets, transport, and local fallback", () => {
    const f = fixture();
    installRouting(f.app, f.api, () => captureDeadlineDestination(true, "disabled", {enabled:true},
        {ready:false,message:"Selected Worker needs native session setup."}));
    assert.throws(() => f.app.queuePrompt(0, 2), /Selected Worker needs native session setup/);
    assert.equal(f.app.nextQueueRequestId, 1);
    assert.equal(f.app.queueItems.length, 0);
    assert.equal(f.calls.length, 0);
});

test("farm readiness does not prevent explicit local Run", async () => {
    const f = fixture();
    installRouting(f.app, f.api, () => captureDeadlineDestination(false, "disabled", {}, {ready:false}));
    await f.app.queuePrompt(0, 1);
    assert.deepEqual(f.calls.map(call => call.route), ["/prompt"]);
});

test("native automatic queue modes exclude Deadline without consulting readiness", () => {
    for (const mode of ["change", "instant"]) {
        assert.equal(captureDeadlineDestination(true, mode, {enabled:true}, {ready:false}), null);
    }
});

test("Distributed nodes cannot reach browser dispatch when Deadline is selected", async () => {
    for (const class_type of ["DistributedCollector", "DeadlineDistributedCollector", "DistributedVideoCollector",
        "DeadlineDistributedVideoCollector", "UltimateSDUpscaleDistributed", "DeadlineUltimateSDUpscaleDistributed"]) {
        const f = fixture();
        f.app.graphToPrompt = async () => ({output:{"1":{class_type,inputs:{}}},workflow:{id:"workflow"}});
        let dispatched = 0;
        f.api.queuePrompt = async () => { dispatched++; };
        installRouting(f.app, f.api, () => ({settings:{enabled:true}}));
        await assert.rejects(f.app.queuePrompt(0, 1), /Use local Run.*Distributed/);
        assert.equal(dispatched, 0);
        assert.equal(f.calls.length, 0);
    }
});

test("local Distributed workflows retain their existing dispatcher", async () => {
    const f = fixture();
    f.app.graphToPrompt = async () => ({output:{"1":{class_type:"DistributedCollector",inputs:{}}},workflow:{id:"workflow"}});
    let dispatched = 0;
    f.api.queuePrompt = async () => { dispatched++; };
    installRouting(f.app, f.api, () => null);
    await f.app.queuePrompt(0, 1);
    assert.equal(dispatched, 1);
});

test("ready farm captures settings once and later readiness loss stops another batch", async () => {
    const f = fixture(), settings = {enabled:true,pool:"test"};
    let ready = true;
    installRouting(f.app, f.api, () => captureDeadlineDestination(true, "disabled", settings,
        {ready,message:"Deadline connection is unavailable."}));
    const captured = captureDeadlineDestination(true, "disabled", settings, {ready:true});
    settings.pool = "updated";
    assert.equal(captured.settings.pool, "test");
    await f.app.queuePrompt(0, 2);
    ready = false;
    assert.throws(() => f.app.queuePrompt(0, 1), /Deadline connection is unavailable/);
    assert.deepEqual(f.calls.map(call => call.route), ["/deadline/farm/submit", "/deadline/farm/submit"]);
});

test("interleaved local and farm queue requests keep their own destination", async () => {
    const f = fixture();
    let farm = true;
    installRouting(f.app, f.api, () => farm ? {settings: {}} : null);
    const first = f.app.queuePrompt(0, 2);
    farm = false;
    const second = f.app.queuePrompt(0, 1);
    await Promise.all([first, second]);
    assert.deepEqual(f.calls.map(call => call.route), ["/deadline/farm/submit", "/deadline/farm/submit", "/prompt"]);
    await f.api.queuePrompt(0, await f.app.graphToPrompt());
    assert.equal(f.calls.at(-1).route, "/prompt");
});

test("partial batch transport failure stops further submissions without local fallback", async () => {
    const f = fixture();
    const fetch = f.api.fetchApi;
    let requests = 0;
    f.api.fetchApi = function (...args) {
        if (++requests === 2) throw new Error("transport lost");
        return fetch.apply(this, args);
    };
    installRouting(f.app, f.api, () => ({settings: {pool: "test"}}));
    await assert.rejects(f.app.queuePrompt(0, 3), /transport lost/);
    assert.equal(f.calls.length, 1);
    assert.equal(f.calls[0].route, "/deadline/farm/submit");
    const firstKey = f.calls[0].body.submission_key;
    await f.app.queuePrompt(0, 1);
    assert.equal(f.calls[1].route, "/deadline/farm/submit");
    assert.notEqual(f.calls[1].body.submission_key, firstKey);
});

test("partial runs are rejected before native widget callbacks", () => {
    const f = fixture();
    installRouting(f.app, f.api, () => ({settings: {}}));
    assert.throws(() => f.app.queuePrompt(0, 1, {queueNodeIds: ["1"]}), /whole workflows/);
    assert.equal(f.app.nextQueueRequestId, 1);
    assert.equal(f.calls.length, 0);
});

test("farm submission requires a finite bounded native batch", () => {
    const f = fixture();
    installRouting(f.app, f.api, () => ({settings: {}}));
    for (const count of [0, -1, Infinity, NaN, 10001, 1.5]) assert.throws(() => f.app.queuePrompt(0, count), /finite batch/);
    assert.equal(f.calls.length, 0);
});

test("extensions that captured an early API method cannot bypass farm routing", async () => {
    const f = fixture();
    const capturedEarly = f.api.queuePrompt.bind(f.api);
    installRouting(f.app, f.api, () => ({settings: {pool: "test"}}));
    f.api.queuePrompt = (...args) => capturedEarly(...args);
    await f.app.queuePrompt(0, 2);
    assert.deepEqual(f.calls.map(call => call.route), ["/deadline/farm/submit", "/deadline/farm/submit"]);
});

test("Submit to Deadline nodes cannot recursively submit or fall back to local execution", async () => {
    const f = fixture();
    f.app.graphToPrompt = async () => ({output: {"1": {class_type: "DeadlineSubmit", inputs: {}}}, workflow: {}});
    installRouting(f.app, f.api, () => ({settings: {}}));
    await f.app.queuePrompt(0);
    assert.equal(f.calls.length, 0);
});

test("active queue loads beyond 200 without a false active count", async () => {
    const calls = [];
    const api = {async fetchApi(route) {
        calls.push(route);
        const offset = Number(new URL(route, "http://local").searchParams.get("offset"));
        const jobs = Array.from({length: offset === 0 ? 200 : 7}, (_, index) => ({id: String(offset + index)}));
        return new Response(JSON.stringify({jobs, pagination: {offset, limit: 200, total: 207, has_more: offset === 0}}));
    }};
    const restore = installActivePagination(api);
    const data = await (await api.fetchApi("/jobs?status=in_progress,pending&limit=200&offset=0")).json();
    assert.equal(data.jobs.length, 207);
    assert.equal(data.pagination.total, 207);
    assert.equal(calls.length, 2);
    restore();
});

test("progress fences late generations and duplicate callbacks per run", () => {
    const progress = new FarmProgress();
    const event = {run_id: "a", generation: 2, sequence: 3, nodes: {"1": {value: 50}}};
    assert.equal(progress.apply(event), true);
    assert.equal(progress.apply({...event}), false);
    assert.equal(progress.apply({...event, generation: 1, sequence: 99}), false);
    assert.equal(progress.apply({...event, run_id: "b", nodes: {"1": {value: 80}}}), true);
    assert.equal(progress.runs.get("a").nodes["1"].value, 50);
    assert.equal(progress.runs.get("b").nodes["1"].value, 80);
    progress.restore([{...event, sequence: 4}]);
    assert.equal(progress.runs.has("b"), false);
});

test("queued Stop advances through cancellation intent and terminal acknowledgement", () => {
    const progress = new FarmProgress();
    const queued = {run_id:"queued", generation:0, sequence:0, status:"pending", cancel_requested:false, nodes:{}};
    assert.equal(progress.apply(queued), true);
    assert.equal(progress.apply({...queued, sequence:1, cancel_requested:true}), true);
    assert.equal(progress.runs.get("queued").cancel_requested, true);
    assert.equal(progress.apply(queued), false);
    assert.equal(progress.apply({...queued, sequence:2, status:"cancelled", cancel_requested:true}), true);
    assert.equal(progress.runs.get("queued").status, "cancelled");
});
