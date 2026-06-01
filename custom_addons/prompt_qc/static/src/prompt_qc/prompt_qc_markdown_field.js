/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useAutoresize } from "@web/core/utils/autoresize";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useRef, useState, onMounted } from "@odoo/owl";

const MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@11.1.1/marked.min.js";
let _markedReady = false;

/** Lazy-load marked once from the CDN (the repo-wide convention; see kensei/valor markdown fields). */
function _loadMarked() {
    if (_markedReady || document.querySelector(`script[src="${MARKED_CDN}"]`)) {
        _markedReady = true;
        return Promise.resolve();
    }
    return new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = MARKED_CDN;
        s.onload = () => {
            _markedReady = true;
            resolve();
        };
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

// Elements that must never survive into the rendered preview.
const _FORBIDDEN_TAGS = new Set([
    "SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "LINK", "META", "BASE", "FORM",
]);
const _DANGEROUS_URL = /^\s*(?:javascript|data|vbscript):/i;

/**
 * Sanitize marked's HTML before it is rendered with markup() + t-out.
 *
 * The value is operator-entered prompt text (attacker-influenceable), and marked@11 does NOT
 * sanitize HTML — raw tags pass through. The repo has no DOMPurify, so we walk the parsed DOM
 * ourselves: drop script/style/iframe/etc., strip on* event handlers, and neutralize
 * javascript:/data:/vbscript: URLs. Parsing then re-serializing preserves markdown rendering
 * fidelity (e.g. code blocks are not double-escaped).
 */
function sanitizeHtml(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_ELEMENT);
    const toRemove = [];
    let node;
    while ((node = walker.nextNode())) {
        if (_FORBIDDEN_TAGS.has(node.tagName)) {
            toRemove.push(node);
            continue;
        }
        for (const attr of [...node.attributes]) {
            const name = attr.name.toLowerCase();
            if (name.startsWith("on")) {
                node.removeAttribute(attr.name);
            } else if (
                (name === "href" || name === "src" || name === "xlink:href") &&
                _DANGEROUS_URL.test(attr.value || "")
            ) {
                node.removeAttribute(attr.name);
            }
        }
    }
    toRemove.forEach((el) => el.remove());
    return doc.body.innerHTML;
}

/** Render markdown to (sanitized) HTML via marked; fall back to escaped plain text if marked is unavailable. */
function renderMarkdown(text) {
    if (!text) return "";
    const lib = window.marked?.default ?? window.marked;
    if (lib && typeof lib.parse === "function") {
        try {
            const html = lib.parse(text);
            return sanitizeHtml(typeof html === "string" ? html : String(html));
        } catch (_e) {
            /* fall through to plain rendering */
        }
    }
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\n/g, "<br/>");
}

/**
 * prompt_qc markdown field.
 *
 * A markdown-friendly editor for long, pasted prompts. In EDIT mode it is an autoresizing
 * monospace <textarea> wired with useInputField + useAutoresize (the valor/berserker
 * markdown_latex convention). In READONLY mode it renders the value as markdown via marked
 * (lazy-loaded from CDN), falling back to escaped plain text when marked is unavailable.
 *
 * Unlike the markdown_latex widgets it is intentionally marked-only — NO KaTeX — so a literal
 * `$` in a prompt (shell var, regex, code) is never reinterpreted as LaTeX math.
 *
 * All hooks are registered UNCONDITIONALLY (mirroring core web.TextField): this field flips
 * readonly across the run lifecycle (draft -> streaming -> done), and useInputField/useAutoresize
 * both guard on a null ref and re-key on the textarea element, so they rewire cleanly when the
 * template swaps between the editor and the preview.
 */
export class PromptQcMarkdownField extends Component {
    static template = "prompt_qc.MarkdownField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.textareaRef = useRef("textarea");
        this.state = useState({ renderedHtml: "" });

        useInputField({
            getValue: () => this.props.record.data[this.props.name] || "",
            refName: "textarea",
            preventLineBreaks: false,
        });
        useAutoresize(this.textareaRef, { minimumHeight: 160 });

        // Keep the readonly preview in sync with the value; lazy-load marked once so the
        // preview upgrades from escaped plain text to rendered markdown when it arrives.
        useRecordObserver((record) => {
            this.state.renderedHtml = renderMarkdown(record.data[this.props.name] || "");
        });
        onMounted(() => {
            _loadMarked()
                .then(() => {
                    this.state.renderedHtml = renderMarkdown(
                        this.props.record.data[this.props.name] || ""
                    );
                })
                .catch(() => {});
        });
    }

    get renderedMarkup() {
        return markup(this.state.renderedHtml || "");
    }
}

export const promptQcMarkdownField = {
    component: PromptQcMarkdownField,
    displayName: _t("Markdown"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("prompt_qc_markdown", promptQcMarkdownField);
