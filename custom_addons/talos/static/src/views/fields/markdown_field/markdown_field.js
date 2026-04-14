/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useAutoresize } from "@web/core/utils/autoresize";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { formatText } from "@web/views/fields/formatters";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useRef, useState } from "@odoo/owl";

const MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js";

function loadScript(src) {
    if (document.querySelector(`script[src="${src}"]`)) {
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = resolve;
        script.onerror = reject;
        document.head.appendChild(script);
    });
}

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

function renderMarkdown(text) {
    if (!text) return "";
    const markedLib = window.marked?.default ?? window.marked;
    if (markedLib && typeof markedLib.parse === "function") {
        try {
            const parsed = markedLib.parse(text);
            return typeof parsed === "string" ? parsed : String(parsed);
        } catch (_e) {
            return escapeHtml(text).replace(/\n/g, "<br/>");
        }
    }
    return escapeHtml(text).replace(/\n/g, "<br/>");
}

export class TalosMarkdownField extends Component {
    static template = "talos.MarkdownField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.textareaRef = useRef("textarea");
        this.state = useState({ renderedHtml: "", libsReady: false });

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
                if (this.state.libsReady) {
                    this.state.renderedHtml = renderMarkdown(raw);
                }
            });
            this._loadLibAndRender();
        }
    }

    async _loadLibAndRender() {
        const raw = formatText(this.props.record.data[this.props.name]);
        if (!raw) {
            this.state.renderedHtml = "";
            this.state.libsReady = true;
            return;
        }
        try {
            await loadScript(MARKED_CDN);
        } catch (e) {
            console.warn("Marked library failed to load:", e);
        }
        this.state.renderedHtml = renderMarkdown(raw);
        this.state.libsReady = true;
    }

    get renderedMarkup() {
        if (!this.state.libsReady || !this.state.renderedHtml) {
            return markup("");
        }
        return markup(this.state.renderedHtml);
    }
}

export const talosMarkdownField = {
    component: TalosMarkdownField,
    displayName: _t("Markdown"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("talos_markdown", talosMarkdownField);
