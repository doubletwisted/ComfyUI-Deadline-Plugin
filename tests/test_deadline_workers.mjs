import {test} from "node:test";
import assert from "node:assert/strict";
import {evaluateWorkers} from "../web/deadline_workers.js";
const settings = {pool:"test", group:"comfy", workers:"", scope:"history"};
const worker = (name, extra = {}) => ({name, host:name, enabled:true, status:"Idle", pools:["test"], groups:["comfy"], ...extra});
const names = list => list.map(item => item.name);
test("pool and group both constrain the optional list", () => {
    const result = evaluateWorkers(settings, {workers:[worker("GPU"), worker("Pool", {pools:["other"]}), worker("Group", {groups:["other"]})]});
    assert.deepEqual(names(result.candidates), ["GPU"]);
});
test("existing Workers need no warm-session mapping", () => {
    assert.equal(evaluateWorkers(settings, {workers:[worker("Cold", {session_configured:false})]}).candidates[0].eligible, true);
});
test("missing and invalid selections survive search", () => {
    const result = evaluateWorkers({...settings, workers:"Removed,Disabled,WrongPool,BadMapping"}, {workers:[
        worker("Disabled", {enabled:false}), worker("WrongPool", {pools:["other"]}), worker("BadMapping", {session_error:"Invalid mapping"})]}, {search:"no-match"});
    assert.equal(result.selected.length, 4);
    assert.equal(result.counts.selectedEligible, 0);
    assert.equal(result.candidates.length, 0);
});
test("names normalize and offline/busy Workers remain queueable", () => {
    const result = evaluateWorkers({...settings, workers:"gpu,GPU,Offline"}, {workers:[worker("GPU", {status:"Rendering"}), worker("Offline", {status:"Offline"})]});
    assert.equal(result.selected.length, 2);
    assert.equal(result.counts.selectedEligible, 2);
    assert.equal(result.counts.selectedAvailable, 0);
});
test("Deadline user determines eligibility independently of history scope", () => {
    const result = evaluateWorkers(settings, {deadline_user:"artist", workers:[worker("Allowed", {scheduling_mode:"UserJobs", user_job_names:["ARTIST"]}), worker("Other", {scheduling_mode:"UserJobs", user_job_names:["other"]}), worker("Unknown", {scheduling_mode:"ListedJobs"})]});
    assert.equal(result.counts.eligible, 1);
    assert.equal(result.candidates.find(item => item.name === "Other").eligible, false);
    assert.deepEqual(result.candidates.find(item => item.name === "Unknown").reasonCodes, ["scheduling"]);
});
test("machine scheduling requires a verified matching user", () => {
    for (const machine_user of ["other", ""]) assert.equal(evaluateWorkers(settings, {deadline_user:"artist", workers:[worker("Machine", {scheduling_mode:"MachineJobs", machine_user})]}).counts.eligible, 0);
});
test("search keeps selected Workers and leaves discovery intact", () => {
    const discovery = {workers:[worker("Selected"), worker("Remote", {host:"SIM01"})]};
    const before = JSON.stringify(discovery);
    const result = evaluateWorkers({...settings, workers:"Selected"}, discovery, {search:"SIM01"});
    assert.deepEqual(names(result.selected), ["Selected"]);
    assert.deepEqual(names(result.candidates), ["Remote"]);
    assert.equal(JSON.stringify(discovery), before);
});
