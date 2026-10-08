import {test} from "node:test";
import assert from "node:assert/strict";
import {fetchFarmDiscovery, farmConnectionStatus} from "../web/deadline_sidebar.js";

test("missing discovery route identifies an old backend instead of farm disconnection", async () => {
    const api = {async fetchApi() {return new Response("Not found",{status:404});}};
    await assert.rejects(fetchFarmDiscovery(api,false), error => error.restartRequired && /Restart ComfyUI/.test(error.message));
    assert.equal(farmConnectionStatus({restart_required:true,connected:false},{enabled:false}),"Restart required");
});
test("repository connectivity is independent of submission being enabled", () => {
    assert.equal(farmConnectionStatus({connected:true},{enabled:false},{connected:false}),"Connected");
    assert.equal(farmConnectionStatus({connected:true},{enabled:true},{connected:false}),"Connected");
    assert.equal(farmConnectionStatus({connected:false},{enabled:true},{connected:true}),"Disconnected");
});
test("real discovery failures retain their connection diagnostic", async () => {
    const api = {async fetchApi() {return new Response("Unavailable",{status:503});}};
    await assert.rejects(fetchFarmDiscovery(api,false), error => !error.restartRequired && /503/.test(error.message));
});
test("successful discovery passes through data, refresh flag and abort signal", async () => {
    const signal=new AbortController().signal;
    const data={connected:true,workers:[{name:"M28"}],pools:["test"]};
    const api={async fetchApi(route,options) {
        assert.equal(route,"/deadline/farm/discovery?refresh=1");assert.equal(options.signal,signal);
        return new Response(JSON.stringify(data));
    }};
    assert.deepEqual(await fetchFarmDiscovery(api,true,signal),data);
});
