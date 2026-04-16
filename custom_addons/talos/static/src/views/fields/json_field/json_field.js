/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";

import { Component, markup, useRef, useState, onMounted, onPatched } from "@odoo/owl";
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
        });
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.editTextareaRef = useRef("editTextarea");

        useRecordObserver((record) => {
            this._updateEntries(formatText(record.data[this.props.name]));
        });
        this._updateEntries(formatText(this.props.record.data[this.props.name]));

        onMounted(() => {
            this._autoResizeEditTextarea();
            this._resumePendingQc();
        });
        onPatched(() => this._autoResizeEditTextarea());
    }

    _autoResizeEditTextarea() {
        const el = this.editTextareaRef.el;
        if (el) {
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 800) + "px";
        }
    }

    _resumePendingQc() {
        for (const entry of this.state.entries) {
            if (entry.qcStatus === "pending") {
                this.onQcEntry(entry.index);
                break;
            }
        }
    }

    _updateEntries(raw) {
        const parsed = parseEntries(raw);
        this.state.entries = parsed.map((entry, idx) => ({
            index: idx,
            sessionId: entry.session_id || `session-${idx + 1}`,
            timestamp: entry.timestamp || "",
            trajectory: entry.trajectory,
            html: markup(renderTrajectoryHtml(entry.trajectory)),
            qcStatus: entry.qc_status || null,
            qcResult: entry.qc_result || null,
        }));
    }

    get hasEntries() {
        return this.state.entries.length > 0;
    }

    get isEditable() {
        return !this.props.readonly;
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

    async _persistQcState(index, qcStatus, qcResult) {
        const raw = this.props.record.data[this.props.name] || "";
        const entries = parseEntries(raw);
        if (index < entries.length) {
            entries[index].qc_status = qcStatus;
            if (qcResult) {
                entries[index].qc_result = qcResult;
            } else {
                delete entries[index].qc_result;
            }
            const newValue = JSON.stringify(entries, null, 2);
            await this.props.record.update({ [this.props.name]: newValue });
            if (this.props.record.resId) {
                await this.orm.write("talos.talos", [this.props.record.resId], {
                    [this.props.name]: newValue,
                });
            }
        }
    }

    async onQcEntry(index) {
        if (this.state.qcRunning >= 0) return;

        const entry = this.state.entries[index];
        if (!entry) return;

        const trajectoryStr = typeof entry.trajectory === "string"
            ? entry.trajectory
            : JSON.stringify(entry.trajectory, null, 2);

        this.state.qcRunning = index;
        this.state.qcCollapsed[index] = false;
        delete this.state.qcResults[index];

        await this._persistQcState(index, "pending", null);

        try {
            const resp = await rpc("/talos/trajectory_qc", { trajectory: trajectoryStr });
            if (resp.error) {
                this.notification.add(resp.error, { type: "danger", sticky: false });
                await this._persistQcState(index, "error", null);
                return;
            }
            if (resp.qc_result) {
                this.state.qcResults[index] = resp.qc_result;
                await this._persistQcState(index, "done", resp.qc_result);
            } else {
                this.notification.add(_t("QC returned no parseable result"), { type: "warning" });
                await this._persistQcState(index, "error", null);
            }
        } catch (e) {
            this.notification.add(
                _t("QC failed: ") + (e.message || String(e)),
                { type: "danger", sticky: false },
            );
            await this._persistQcState(index, "error", null);
        } finally {
            this.state.qcRunning = -1;
        }
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
