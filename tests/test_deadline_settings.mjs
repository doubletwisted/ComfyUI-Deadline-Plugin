import {test} from "node:test";
import assert from "node:assert/strict";
import {DeadlineSettings} from "../web/deadline_settings.js";

function fixture() {
    let server = {enabled: false, pool: "test", workers: "M28", priority: 50};
    let fail = false;
    const api = {async fetchApi(route, options = {}) {
        if (options.method === "POST") {
            const body = JSON.parse(options.body);
            assert.deepEqual(body.expected_settings, server);
            if (fail) return new Response(JSON.stringify({error: {message: "Share unavailable"}}), {status:503});
            server = body.settings; return new Response(JSON.stringify(server));
        }
        return new Response(JSON.stringify({settings:server, health:{connected:true}}));
    }};
    const saved = [];
    return {model:new DeadlineSettings(api, value => saved.push(value)), saved,
        external(value) {server = {...server, ...value};}, fail() {fail = true;}};
}
test("refresh preserves unsaved settings and missing Worker names", async () => {
    const {model} = fixture(); await model.load(); model.edit("workers", "M28,RemovedWorker"); model.edit("priority", 85);
    await model.load(); assert.equal(model.draft.priority, 85); assert.equal(model.draft.workers, "M28,RemovedWorker");
    assert.equal(model.conflict, false); assert.equal(model.dirty, true);
    model.revert(); assert.equal(model.draft.workers, "M28"); assert.equal(model.dirty, false);
});
test("external edits require explicit revert before save", async () => {
    const f = fixture(); await f.model.load(); f.model.edit("priority", 85); f.external({priority:60});
    await f.model.load(); await f.model.load(); assert.equal(f.model.conflict, true);
    assert.equal(await f.model.save(), false); assert.equal(f.model.draft.priority,85);
    f.model.revert(); assert.equal(f.model.draft.priority,60); assert.equal(f.model.conflict,false);
});
test("Apply persists one atomic draft and failed saves preserve it", async () => {
    const f = fixture(); await f.model.load(); f.model.edit("priority",90);
    assert.equal(await f.model.save(),true); assert.equal(f.model.dirty,false); assert.equal(f.saved.at(-1).priority,90);
    f.model.edit("priority",80); f.fail(); assert.equal(await f.model.save(),false);
    assert.equal(f.model.draft.priority,80); assert.equal(f.model.saved.priority,90); assert.match(f.model.error,/Share unavailable/);
});
test("closing a sidebar aborts late settings updates", async () => {
    const f = fixture(); const controller = new AbortController(); controller.abort();
    await assert.rejects(f.model.load(controller.signal), /aborted/); assert.equal(f.model.saved,null);
});
test("a save conflict reloads the authoritative settings before Revert", async () => {
    let server = {priority:50};
    const model = new DeadlineSettings({async fetchApi(route, options = {}) {
        return options.method === "POST" ? new Response(JSON.stringify({error:{type:"deadline_settings_conflict",message:"Changed elsewhere"}}),{status:409}) :
            new Response(JSON.stringify({settings:server}));
    }},()=>{});
    await model.load(); model.edit("priority",90); server = {priority:60};
    assert.equal(await model.save(),false); assert.equal(model.conflict,true); assert.equal(model.draft.priority,90);
    model.revert(); assert.equal(model.draft.priority,60);
});

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
}

test("a refresh started before Apply cannot restore old saved settings or routing", async () => {
    const oldRead = deferred();
    let reads = 0, server = {priority: 50};
    const routed = [];
    const model = new DeadlineSettings({async fetchApi(route, options = {}) {
        if (options.method === "POST") {
            server = JSON.parse(options.body).settings;
            return new Response(JSON.stringify(server));
        }
        const snapshot = {...server};
        if (++reads === 2) await oldRead.promise;
        return new Response(JSON.stringify({settings: snapshot}));
    }}, settings => routed.push(settings));
    await model.load();
    model.edit("priority", 90);
    const refresh = model.load();
    assert.equal(await model.save(), true);
    oldRead.resolve();
    await refresh;
    assert.equal(server.priority, 90);
    assert.equal(model.saved.priority, 90);
    assert.equal(model.draft.priority, 90);
    assert.equal(model.dirty, false);
    assert.equal(routed.at(-1).priority, 90);
});

test("a refresh started during Apply cannot overwrite the committed response", async () => {
    const post = deferred(), oldRead = deferred();
    let reads = 0, server = {priority: 50};
    const model = new DeadlineSettings({async fetchApi(route, options = {}) {
        if (options.method === "POST") {
            await post.promise;
            server = JSON.parse(options.body).settings;
            return new Response(JSON.stringify(server));
        }
        const snapshot = {...server};
        if (++reads === 2) await oldRead.promise;
        return new Response(JSON.stringify({settings: snapshot}));
    }}, () => {});
    await model.load();
    model.edit("priority", 90);
    const save = model.save();
    const refresh = model.load();
    post.resolve();
    assert.equal(await save, true);
    oldRead.resolve();
    await refresh;
    assert.equal(model.saved.priority, 90);
    assert.equal(model.conflict, false);
});

test("out-of-order refreshes accept only the latest request", async () => {
    const oldRead = deferred();
    let reads = 0;
    const model = new DeadlineSettings({async fetchApi() {
        const priority = ++reads === 1 ? 50 : 60;
        if (reads === 1) await oldRead.promise;
        return new Response(JSON.stringify({settings: {priority}}));
    }}, () => {});
    const oldRefresh = model.load();
    await model.load();
    oldRead.resolve();
    await oldRefresh;
    assert.equal(model.saved.priority, 60);
    assert.equal(model.draft.priority, 60);
});
