/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useAutoresize } from "@web/core/utils/autoresize";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useRef, useState } from "@odoo/owl";

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

function formatJsonContent(raw) {
    if (!raw) return "";
    const trimmed = raw.trim();
    try {
        const parsed = JSON.parse(trimmed);
        const pretty = JSON.stringify(parsed, null, 2);
        return `<pre class="talos-json-block"><code>${syntaxHighlightJsonToHtml(pretty)}</code></pre>`;
    } catch (_e) {
        return `<pre class="talos-json-block"><code>${escapeHtml(trimmed)}</code></pre>`;
    }
}

export class TalosJsonField extends Component {
    static template = "talos.JsonField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.textareaRef = useRef("textarea");
        this.state = useState({ renderedHtml: "" });

        if (!this.props.readonly) {
            useInputField({
                getValue: () => formatText(this.props.record.data[this.props.name]),
                refName: "textarea",
                preventLineBreaks: false,
            });
            useAutoresize(this.textareaRef, { minimumHeight: 50 });
        } else {
            useRecordObserver((record) => {
                const raw = formatText(record.data[this.props.name]);
                this.state.renderedHtml = formatJsonContent(raw);
            });
            this.state.renderedHtml = formatJsonContent(
                formatText(this.props.record.data[this.props.name])
            );
        }
    }

    get renderedMarkup() {
        return markup(this.state.renderedHtml || "");
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
