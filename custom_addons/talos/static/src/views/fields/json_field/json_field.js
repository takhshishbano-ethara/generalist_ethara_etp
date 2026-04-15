/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useAutoresize } from "@web/core/utils/autoresize";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useService } from "@web/core/utils/hooks";

import { Component, markup, useRef, useState } from "@odoo/owl";
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
        this.textareaRef = useRef("textarea");
        this.state = useState({ entries: [], deleting: -1 });
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");

        if (!this.props.readonly) {
            useInputField({
                getValue: () => formatText(this.props.record.data[this.props.name]),
                refName: "textarea",
                preventLineBreaks: false,
            });
            useAutoresize(this.textareaRef, { minimumHeight: 50 });
        } else {
            useRecordObserver((record) => {
                this._updateEntries(formatText(record.data[this.props.name]));
            });
            this._updateEntries(formatText(this.props.record.data[this.props.name]));
        }
    }

    _updateEntries(raw) {
        const parsed = parseEntries(raw);
        this.state.entries = parsed.map((entry, idx) => ({
            index: idx,
            sessionId: entry.session_id || `session-${idx + 1}`,
            timestamp: entry.timestamp || "",
            html: markup(renderTrajectoryHtml(entry.trajectory)),
        }));
    }

    get hasEntries() {
        return this.state.entries.length > 0;
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
