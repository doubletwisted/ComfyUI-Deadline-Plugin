const same = (a, b) => JSON.stringify(Object.keys(a).sort().map(k => [k, a[k]])) === JSON.stringify(Object.keys(b).sort().map(k => [k, b[k]]));

export class DeadlineSettings {
    constructor(api, onSaved, onChange = () => {}) {
        Object.assign(this, {api, onSaved, onChange, saved: null, draft: {}, conflict: false, saving: false, error: "", loadVersion: 0});
    }
    get dirty() { return this.saved !== null && !same(this.saved, this.draft); }
    edit(key, value) { this.draft = {...this.draft, [key]: value}; this.error = ""; this.onChange(); }
    accept(settings) {
        if (this.dirty || this.saving) this.conflict ||= !same(this.saved, settings);
        else { this.draft = {...settings}; this.conflict = false; this.error = ""; }
        this.saved = {...settings}; this.onSaved(settings); this.onChange();
    }
    revert() { this.draft = {...this.saved}; this.conflict = false; this.error = ""; this.onChange(); }
    async load(signal) {
        const version = ++this.loadVersion;
        const response = await this.api.fetchApi("/deadline/farm/settings", {signal});
        if (!response.ok) throw new Error("Could not load Deadline settings");
        const value = await response.json();
        if (signal?.aborted) throw new DOMException("Request aborted", "AbortError");
        if (version === this.loadVersion) this.accept(value.settings);
        return value.health;
    }
    async save() {
        if (!this.saved || this.conflict || this.saving || !this.dirty) return false;
        this.loadVersion++;
        this.saving = true; this.error = ""; this.onChange();
        try {
            const result = await this.api.fetchApi("/deadline/farm/settings", {method: "POST", headers: {"Content-Type": "application/json"},
                body: JSON.stringify({settings: this.draft, expected_settings: this.saved})});
            const value = await result.json();
            if (value.error?.type === "deadline_settings_conflict") {
                this.conflict = true;
                await this.load();
            }
            if (!result.ok) throw new Error(value.error?.message || "Could not save Deadline settings");
            this.saved = {...value}; this.draft = {...value}; this.conflict = false; this.onSaved(value); return true;
        } catch (error) { this.error = error.message; return false; }
        finally { this.loadVersion++; this.saving = false; this.onChange(); }
    }
}
