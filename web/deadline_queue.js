import {app} from "../../scripts/app.js";
import {api} from "../../scripts/api.js";
import {FarmProgress, installActivePagination, installRouting} from "./deadline_routing.js";
import {captureDeadlineDestination, inspectNativeQueue, installNativeControls} from "./native_queue_compat.js";
import {installDeadlineSidebar} from "./deadline_sidebar.js";

app.registerExtension({
    name: "Deadline.NativeQueue",
    async setup() {
        const response = await api.fetchApi("/deadline/farm/capabilities");
        if (!response.ok) return;
        const capabilities = await response.json();
        let configured = {};
        const sidebar = capabilities.mode !== "prototype" ? installDeadlineSidebar(app, api, capabilities, value => { configured = value; }) : null;
        if (capabilities.mode === "unsupported") {
            app.extensionManager.toast.addAlert(capabilities.message);
            return;
        }
        if (sidebar) {
            try { await sidebar.model.load(); }
            catch (error) { sidebar.model.error = error.message; }
        }
        if (!capabilities.ready && capabilities.mode !== "native") return;
        let stats;
        try {
            const response = await api.fetchApi("/system_stats");
            if (!response.ok) throw new Error(`System information unavailable (${response.status})`);
            stats = await response.json();
        } catch (error) {
            sidebar?.setRun(null, [error.message]);
            return;
        }
        const version = stats.system.comfy_package_versions.find(item => item.name === "comfyui-frontend-package")?.installed;
        // Extension setup runs before the action bar and lazy preview store
        // mount. Return now so ComfyUI can finish booting, then check the seam.
        let checks = 0;
        function initialize() {
        const state = inspectNativeQueue(app, version);
        if (version === "1.53.10" && state.problems.length && ++checks < 120) {
            setTimeout(initialize, 250);
            return;
        }
        app.deadlineQueue = {capabilities, problems: state.problems, stale: false};
        if (state.problems.length) {
            sidebar?.setRun(null, state.problems);
            app.extensionManager.toast.addAlert(state.problems.join(" "));
            return;
        }
        if (capabilities.mode === "prototype") app.extensionManager.toast.addAlert(capabilities.message);
        let farmSelected = false;
        let stopped = false;
        let timer;
        const progress = new FarmProgress();
        const selectDestination = value => { farmSelected = value; app.deadlineQueue.selected = value; };
        const getReadiness = () => app.deadlineQueue.stale ? {ready:false,message:"Deadline connection is unavailable. Refresh the Deadline sidebar before submitting."} : sidebar?.getReadiness() || {ready:true};
        const getDestination = () => farmSelected && state.settings.mode === "disabled" ? {settings:configured} : null;
        const captureDestination = () => {
            const readiness = getReadiness();
            if (getDestination() && !readiness.ready) app.extensionManager.toast.addAlert(readiness.message);
            return captureDeadlineDestination(farmSelected, state.settings.mode, configured, readiness);
        };
        const restorePagination = installActivePagination(api);
        const restoreRouting = installRouting(app, api, captureDestination);
        const retryKeys = new Map();
        const retry = async id => {
            if (!retryKeys.has(id)) retryKeys.set(id, crypto.randomUUID());
            const response = await api.fetchApi(`/deadline/farm/${id}/retry`, {
                method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({submission_key: retryKeys.get(id)})
            });
            if (!response.ok) { app.extensionManager.toast.addAlert(`Deadline retry failed: ${await response.text()}`); return; }
            retryKeys.delete(id);
            await state.queue.update();
        };
        const stop = async id => {
            const response = await api.fetchApi(`/jobs/${id}/cancel`, {method:"POST"});
            if (!response.ok) throw new Error(await response.text());
            const {cancelled} = await response.json();
            const event = progress.runs.get(id);
            if (cancelled && event && !["completed", "failed", "cancelled"].includes(event.status)) {
                progress.apply({...event, cancel_requested:true, sequence:Math.floor(event.sequence / 4) * 4 + 1});
            }
            controls.render();
            try { await state.queue.update(); }
            catch (error) { console.warn("[Deadline] Stop requested; native Jobs refresh failed:", error); }
        };
        const controls = installNativeControls({app, state, progress, getDestination, getReadiness, openSetup:sidebar?.openSetup, selectDestination, retry, stop});
        sidebar?.setRun(() => {
            if (!getReadiness().ready) { sidebar.openSetup(); return; }
            state.settings.mode = "disabled";
            selectDestination(true);
            controls.render();
            const button = document.querySelector('[data-testid="queue-button"]');
            if (button && !button.disabled) button.click();
        });
        const ownedIds = new Set();
        async function poll() {
            try {
                const response = await api.fetchApi("/deadline/farm/progress");
                if (!response.ok) throw new Error(`Farm progress unavailable (${response.status})`);
                const {runs, health, readiness} = await response.json();
                if (stopped) return;
                app.deadlineQueue.health = health;
                app.deadlineQueue.readiness = readiness;
                sidebar?.updateHealth(health, readiness);
                if (health && !health.connected && configured.enabled) throw new Error(health.message);
                if (stopped) return;
                progress.restore(runs);
                const next = {...state.execution.nodeProgressStatesByJob};
                for (const id of ownedIds) {
                    if (!progress.runs.has(id)) { delete next[id]; state.previews.clearPreview(id); ownedIds.delete(id); }
                }
                for (const event of progress.runs.values()) {
                    ownedIds.add(event.run_id);
                    if (event.status === "in_progress") {
                        next[event.run_id] = event.nodes;
                        if (event.preview_url) state.previews.setPreviewUrl(event.run_id, event.preview_url);
                    } else {
                        delete next[event.run_id];
                        state.previews.clearPreview(event.run_id);
                    }
                }
                // Deliberately leave activeJobId, global nodeProgressStates,
                // canvas output store and legacy execution events untouched.
                state.execution.nodeProgressStatesByJob = next;
                app.deadlineQueue.stale = false;
                controls.render();
            } catch (error) {
                if (!app.deadlineQueue.stale) {
                    console.warn("[Deadline] Keeping stale farm state:", error);
                }
                app.deadlineQueue.stale = true;
                sidebar?.updateHealth({connected:false,message:error.message}, {ready:false,code:"repository",message:error.message});
                controls.render();
            } finally {
                try { await state.queue.update(); }
                catch (error) { console.warn("[Deadline] Native Jobs refresh failed:", error); }
                if (!stopped) timer = setTimeout(poll, 2000);
            }
        }
        const onChanged = () => { state.queue.update(); };
        api.addEventListener("deadline:queue-changed", onChanged);
        const dispose = () => {
            stopped = true;
            clearTimeout(timer);
            controls.dispose();
            sidebar?.dispose();
            restoreRouting();
            restorePagination();
            api.removeEventListener("deadline:queue-changed", onChanged);
            window.removeEventListener("pagehide", dispose);
            const next = {...state.execution.nodeProgressStatesByJob};
            for (const id of ownedIds) { delete next[id]; state.previews.clearPreview(id); }
            state.execution.nodeProgressStatesByJob = next;
        };
        window.addEventListener("pagehide", dispose);
        app.deadlineQueue.dispose = dispose;
        poll();
        }
        setTimeout(initialize, 0);
    }
});
