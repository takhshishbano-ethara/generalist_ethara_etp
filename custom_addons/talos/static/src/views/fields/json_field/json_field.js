/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

import { Component, markup, useRef, useState, onMounted, onPatched, onWillPatch, onWillUnmount } from "@odoo/owl";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

function syntaxHighlightJsonToHtml(jsonStr) {
    return escapeHtml(jsonStr).replace(
        /("(?:\\.|[^"\\])*")\s*:/g,
        '<span class="json-key">$1</span>:'
    ).replace(
        /:\s*("(?:\\.|[^"\\])*")/g,
        ': <span class="json-string">$1</span>'
    ).replace(
        /:\s*(\d+(?:\.\d+)?)/g,
        ': <span class="json-number">$1</span>'
    ).replace(
        /:\s*(true|false)/g,
        ': <span class="json-boolean">$1</span>'
    ).replace(
        /:\s*(null)/g,
        ': <span class="json-null">$1</span>'
    );
}

function parseEntries(raw) {
    if (!raw) return [];
    const trimmed = raw.trim();
    if (!trimmed) return [];
    try {
        const parsed = JSON.parse(trimmed);
        if (Array.isArray(parsed)) {
            return parsed;
        }
        return [{ session_id: "legacy", timestamp: "", trajectory: parsed }];
    } catch (_e) {
        return [{ session_id: "raw", timestamp: "", trajectory: trimmed }];
    }
}

function renderTrajectoryHtml(trajectory) {
    if (typeof trajectory === "string") {
        return `<pre class="talos-json-block"><code>${escapeHtml(trajectory)}</code></pre>`;
    }
    const pretty = JSON.stringify(trajectory, null, 2);
    return `<pre class="talos-json-block"><code>${syntaxHighlightJsonToHtml(pretty)}</code></pre>`;
}

export class TalosJsonField extends Component {
    static template = "talos.JsonField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            entries: [],
            deleting: -1,
            editingIndex: -1,
            editBuffer: "",
            editError: "",
            qcRunning: -1,
            qcResults: {},
            qcExpandedChecks: {},
            qcCollapsed: {},
            taskDescGenerating: -1,
        });
        this._qcPollingTimers = {};
        this._taskDescPollingTimers = {};
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.editTextareaRef = useRef("editTextarea");
        this.rootRef = useRef("root");
        this._savedScrollPositions = null;
        this._lastRawValue = null;

        useRecordObserver((record) => {
            this._updateEntries(formatText(record.data[this.props.name]));
        });
        this._updateEntries(formatText(this.props.record.data[this.props.name]));

        this._onGoldenStatusChanged = (ev) => {
            this._handleGoldenStatusChanged(ev.detail);
        };
        if (this.isGoldenField) {
            this.env.bus.addEventListener(
                "TALOS:GOLDEN_STATUS_CHANGED",
                this._onGoldenStatusChanged,
            );
        }

        onMounted(() => {
            this._autoResizeEditTextarea();
            this._resumePendingQc();
        });
        onWillPatch(() => this._saveScrollPositions());
        onPatched(() => {
            this._restoreScrollPositions();
            this._autoResizeEditTextarea();
        });
        onWillUnmount(() => {
            this._clearAllPolling();
            if (this.isGoldenField) {
                this.env.bus.removeEventListener(
                    "TALOS:GOLDEN_STATUS_CHANGED",
                    this._onGoldenStatusChanged,
                );
            }
        });
    }

    _autoResizeEditTextarea() {
        const el = this.editTextareaRef.el;
        if (el) {
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 800) + "px";
        }
    }

    _saveScrollPositions() {
        const root = this.rootRef.el;
        if (!root) return;
        const positions = [];
        const bodies = root.querySelectorAll(".talos-json-entry-body");
        for (const body of bodies) {
            positions.push(body.scrollTop);
        }
        const scrollParent = root.closest(".o_field_widget") || root;
        this._savedScrollPositions = {
            bodies: positions,
            rootTop: scrollParent.scrollTop,
        };
    }

    _restoreScrollPositions() {
        const saved = this._savedScrollPositions;
        if (!saved) return;
        this._savedScrollPositions = null;

        const root = this.rootRef.el;
        if (!root) return;

        const bodies = root.querySelectorAll(".talos-json-entry-body");
        for (let i = 0; i < bodies.length && i < saved.bodies.length; i++) {
            if (saved.bodies[i]) {
                bodies[i].scrollTop = saved.bodies[i];
            }
        }
        const scrollParent = root.closest(".o_field_widget") || root;
        if (saved.rootTop) {
            scrollParent.scrollTop = saved.rootTop;
        }
    }

    _resumePendingQc() {
        for (const entry of this.state.entries) {
            if (entry.qcStatus === "pending") {
                this._startQcPolling(entry.index);
            }
            if (entry.taskDescriptionStatus === "pending") {
                this.state.taskDescGenerating = entry.index;
                this._startTaskDescPolling(entry.index);
            }
        }
    }

    _clearAllPolling() {
        for (const key of Object.keys(this._qcPollingTimers)) {
            clearInterval(this._qcPollingTimers[key]);
        }
        this._qcPollingTimers = {};
        for (const key of Object.keys(this._taskDescPollingTimers)) {
            clearInterval(this._taskDescPollingTimers[key]);
        }
        this._taskDescPollingTimers = {};
    }

    async _handleGoldenStatusChanged(payload) {
        if (!payload || !payload.task_id) return;
        if (payload.task_id !== this.props.record.resId) return;
        try {
            await this.props.record.load();
        } catch (e) {
            console.warn("[TalosJsonField] Failed to reload after golden status change:", e);
        }
    }

    _startQcPolling(index) {
        if (this._qcPollingTimers[index]) return;
        this._qcPollingTimers[index] = setInterval(async () => {
            try {
                await this.props.record.load();
                const raw = formatText(this.props.record.data[this.props.name]);
                const entries = parseEntries(raw);
                if (index < entries.length) {
                    const status = entries[index].qc_status;
                    if (status && status !== "pending") {
                        clearInterval(this._qcPollingTimers[index]);
                        delete this._qcPollingTimers[index];
                        if (status === "done" && entries[index].qc_result) {
                            this.state.qcResults[index] = entries[index].qc_result;
                            this.state.qcCollapsed[index] = false;
                        }
                        this.state.qcRunning = -1;
                    }
                }
            } catch (_e) {
                // network hiccup — keep polling
            }
        }, 5000);
    }

    _startTaskDescPolling(index) {
        if (this._taskDescPollingTimers[index]) return;
        this._taskDescPollingTimers[index] = setInterval(async () => {
            try {
                await this.props.record.load();
                const raw = formatText(this.props.record.data[this.props.name]);
                const entries = parseEntries(raw);
                if (index < entries.length) {
                    const status = entries[index].task_description_status;
                    if (status && status !== "pending") {
                        clearInterval(this._taskDescPollingTimers[index]);
                        delete this._taskDescPollingTimers[index];
                        this.state.taskDescGenerating = -1;
                    }
                }
            } catch (_e) {
            }
        }, 5000);
    }

    _updateEntries(raw) {
        if (raw === this._lastRawValue) return;
        this._lastRawValue = raw;
        const parsed = parseEntries(raw);
        this.state.entries = parsed.map((entry, idx) => ({
            index: idx,
            sessionId: entry.session_id || `session-${idx + 1}`,
            timestamp: entry.timestamp || "",
            trajectory: entry.trajectory,
            html: markup(renderTrajectoryHtml(entry.trajectory)),
            qcStatus: entry.qc_status || null,
            qcResult: entry.qc_result || null,
            taskDescriptionStatus: entry.task_description_status || null,
        }));
    }

    get hasEntries() {
        return this.state.entries.length > 0;
    }

    get isEditable() {
        return !this.props.readonly;
    }

    get isAnyActionRunning() {
        return this.state.qcRunning >= 0 || this.state.taskDescGenerating >= 0;
    }

    get isGoldenField() {
        return this.props.name === "golden_trajectory";
    }

    onDeleteEntry(index) {
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete Trajectory Session"),
            body: _t("Are you sure you want to delete this generated trajectory session? This action cannot be undone."),
            confirmLabel: _t("Delete"),
            confirmClass: "btn-danger",
            cancelLabel: _t("Cancel"),
            confirm: async () => {
                this.state.deleting = index;
                try {
                    await this.orm.call(
                        "talos.talos",
                        "action_delete_trajectory_entry",
                        [this.props.record.resId],
                        { field_name: this.props.name, entry_index: index }
                    );
                    await this.props.record.load();
                } finally {
                    this.state.deleting = -1;
                }
            },
        });
    }

    onEditEntry(index) {
        const entry = this.state.entries[index];
        let text;
        if (typeof entry.trajectory === "string") {
            text = entry.trajectory;
        } else {
            text = JSON.stringify(entry.trajectory, null, 2);
        }
        this.state.editingIndex = index;
        this.state.editBuffer = text;
        this.state.editError = "";
    }

    onEditInput(ev) {
        this.state.editBuffer = ev.target.value;
        this.state.editError = "";
        ev.target.style.height = "auto";
        ev.target.style.height = Math.min(ev.target.scrollHeight, 800) + "px";
    }

    onSaveEntry() {
        const idx = this.state.editingIndex;
        if (idx < 0) return;

        let newTrajectory;
        try {
            newTrajectory = JSON.parse(this.state.editBuffer);
        } catch (e) {
            this.state.editError = "Invalid JSON: " + e.message;
            return;
        }

        const raw = this.props.record.data[this.props.name] || "";
        let entries = parseEntries(raw);
        if (idx < entries.length) {
            entries[idx].trajectory = newTrajectory;
        }

        const newValue = JSON.stringify(entries, null, 2);
        this.props.record.update({ [this.props.name]: newValue });

        this.state.editingIndex = -1;
        this.state.editBuffer = "";
        this.state.editError = "";
    }

    onCancelEdit() {
        this.state.editingIndex = -1;
        this.state.editBuffer = "";
        this.state.editError = "";
    }

    async onQcEntry(index) {
        if (this.state.qcRunning >= 0 || this.state.taskDescGenerating >= 0) return;

        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) {
            this.notification.add(_t("Save the record first"), { type: "warning" });
            return;
        }

        this.state.qcRunning = index;
        this.state.qcCollapsed[index] = false;
        delete this.state.qcResults[index];

        try {
            const resp = await rpc("/talos/trajectory_qc", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
            });
            if (resp.error) {
                this.notification.add(resp.error, { type: "danger", sticky: false });
                this.state.qcRunning = -1;
                return;
            }
            this._startQcPolling(index);
        } catch (e) {
            this.notification.add(
                _t("QC failed: ") + (e.message || String(e)),
                { type: "danger", sticky: false },
            );
            this.state.qcRunning = -1;
        }
    }

    async onGenerateTaskDesc(index) {
        if (this.state.taskDescGenerating >= 0 || this.state.qcRunning >= 0) return;

        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) {
            this.notification.add(_t("Save the record first"), { type: "warning" });
            return;
        }

        this.state.taskDescGenerating = index;

        try {
            const resp = await rpc("/talos/generate_task_description", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
            });
            if (resp.error) {
                this.notification.add(resp.error, { type: "danger", sticky: false });
                this.state.taskDescGenerating = -1;
                return;
            }
            this._startTaskDescPolling(index);
        } catch (e) {
            this.notification.add(
                _t("Description generation failed: ") + (e.message || String(e)),
                { type: "danger", sticky: false },
            );
            this.state.taskDescGenerating = -1;
        }
    }

    async onAbortQc(index) {
        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) return;

        try {
            await rpc("/talos/abort_trajectory_action", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
                action_type: "qc",
            });
        } catch (_e) { /* best-effort */ }

        if (this._qcPollingTimers[index]) {
            clearInterval(this._qcPollingTimers[index]);
            delete this._qcPollingTimers[index];
        }
        this.state.qcRunning = -1;
        // Reset local entry status so buttons return to initial state immediately
        if (this.state.entries[index]) {
            this.state.entries[index].qcStatus = "aborted";
        }
        this.notification.add(_t("QC aborted"), { type: "info" });
        // Reload record to sync DB state
        try { await this.props.record.load(); } catch (_e) { /* ignore */ }
    }

    async onAbortTaskDesc(index) {
        const recordId = this.props.record.resId;
        const fieldName = this.props.name;
        if (!recordId) return;

        try {
            await rpc("/talos/abort_trajectory_action", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: index,
                action_type: "task_description",
            });
        } catch (_e) { /* best-effort */ }

        if (this._taskDescPollingTimers[index]) {
            clearInterval(this._taskDescPollingTimers[index]);
            delete this._taskDescPollingTimers[index];
        }
        this.state.taskDescGenerating = -1;
        // Reset local entry status so buttons return to initial state immediately
        if (this.state.entries[index]) {
            this.state.entries[index].taskDescriptionStatus = "aborted";
        }
        this.notification.add(_t("Description generation aborted"), { type: "info" });
        // Reload record to sync DB state
        try { await this.props.record.load(); } catch (_e) { /* ignore */ }
    }

    onToggleQcChecks(index) {
        this.state.qcExpandedChecks[index] = !this.state.qcExpandedChecks[index];
    }

    onShowQc(index) {
        this.state.qcCollapsed[index] = false;
    }

    onDismissQc(index) {
        // Just collapse the panel — don't clear persisted QC state
        this.state.qcCollapsed = this.state.qcCollapsed || {};
        this.state.qcCollapsed[index] = true;
        this.state.qcExpandedChecks[index] = false;
    }

    severityClass(severity) {
        const map = { low: "success", medium: "warning", high: "danger", critical: "danger" };
        return map[severity] || "secondary";
    }

    severityIcon(severity) {
        const map = {
            low: "fa-check-circle",
            medium: "fa-exclamation-circle",
            high: "fa-exclamation-triangle",
            critical: "fa-ban",
        };
        return map[severity] || "fa-question-circle";
    }
}

export const talosJsonField = {
    component: TalosJsonField,
    displayName: _t("JSON Viewer"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("talos_json", talosJsonField);
