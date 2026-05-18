/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useState, onMounted } from "@odoo/owl";

const MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js";
let _markedReady = false;

function _loadMarked() {
    if (_markedReady || document.querySelector(`script[src="${MARKED_CDN}"]`)) {
        _markedReady = true;
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = MARKED_CDN;
        s.onload = () => { _markedReady = true; resolve(); };
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

function renderMarkdown(text) {
    if (!text) return "";
    const lib = window.marked?.default ?? window.marked;
    if (lib && typeof lib.parse === "function") {
        try {
            const html = lib.parse(text);
            return typeof html === "string" ? html : String(html);
        } catch (_) { /* fall through */ }
    }
    return text.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
}

export class SkollMarkdownField extends Component {
    static template = "skoll.MarkdownField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({ renderedHtml: "" });

        useRecordObserver((record) => {
            const raw = record.data[this.props.name];
            this.state.renderedHtml = renderMarkdown(raw || "");
        });

        onMounted(() => {
            _loadMarked().then(() => {
                const raw = this.props.record.data[this.props.name];
                this.state.renderedHtml = renderMarkdown(raw || "");
            }).catch(() => {});
        });

        const raw = this.props.record.data[this.props.name];
        this.state.renderedHtml = renderMarkdown(raw || "");
    }

    get renderedMarkup() {
        return markup(this.state.renderedHtml || "");
    }
}

export const skollMarkdownField = {
    component: SkollMarkdownField,
    displayName: _t("Markdown"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("skoll_markdown", skollMarkdownField);
