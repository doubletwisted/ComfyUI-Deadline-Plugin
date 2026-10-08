import {test} from "node:test";
import assert from "node:assert/strict";
import {sidebarReadiness} from "../web/deadline_sidebar.js";
import {evaluateWorkers} from "../web/deadline_workers.js";
const settings = {root:"shared-root",enabled:true,workers:"",pool:"test",group:"comfy"};
const discovery = {connected:true,plugin_available:true,workers:[]};
function state(config = settings, farm = discovery, server = {ready:true}, dirty = false) {
    return sidebarReadiness(config, farm, evaluateWorkers(config, farm), server, dirty);
}
test("pool/group scheduling needs no Worker allowlist or owner installation", () => {
    assert.equal(state().ready, true);
    assert.match(state().message, /native Run/);
});
test("deployment problems direct artists to the administrator guide", () => {
    for (const result of [state({...settings, root:""}), state({...settings, enabled:false}), state(settings, {...discovery, plugin_available:false})]) {
        assert.equal(result.action, "guide");
        assert.equal(result.ready, undefined);
        assert.doesNotMatch(result.message, /Set up this machine/);
    }
});
test("failed storage verification cannot be hidden by a nonempty path", () => {
    const result = state(settings, discovery, {ready:false,code:"storage",message:"Shared folder unavailable"});
    assert.equal(result.ready, undefined);
    assert.equal(result.message, "Shared folder unavailable");
    assert.equal(result.action, "guide");
});
test("pending saves block submission until new settings are accepted", () => {
    assert.equal(state(settings, discovery, {ready:true}, true).ready, undefined);
});
test("invalid optional restrictions remain visible and block submission", () => {
    const result = state({...settings, workers:"Removed"});
    assert.equal(result.ready, undefined);
    assert.equal(result.action, "workers");
    assert.match(result.message, /Removed/);
});
test("connection and restart diagnostics remain actionable", () => {
    assert.equal(state(settings, {...discovery, connected:false}).action, "refresh");
    assert.equal(state(settings, {...discovery, restart_required:true}).title, "Restart required");
});
