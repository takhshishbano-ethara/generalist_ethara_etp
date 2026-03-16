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
const KATEX_JS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js";
const KATEX_CSS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css";

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

function loadCSS(href) {
    if (document.querySelector(`link[href="${href}"]`)) {
        return;
    }
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = href;
    document.head.appendChild(link);
}

/**
 * Escape $ only when followed by a digit, so currency like $100 renders as $100
 * while math $x + y$ is left untouched. Apply before markdown/math parsing.
 */
function escapeCurrencyDollars(text) {
    return text.replace(/\$(?=\d)/g, "\\$");
}

/**
 * Get KaTeX from global (UMD may expose as katex or katex.default).
 */
function getKaTeX() {
    const k = window.katex ?? window.KaTeX;
    return k?.default ?? k;
}

const MATH_PLACEHOLDER_PREFIX = "\u200B\u200B\u200B"; // zero-width, unique
const MATH_PLACEHOLDER_SUFFIX = "\u200C\u200C\u200C";

/**
 * 1. Extract math from raw text and replace with placeholders (so Marked doesn't alter them).
 * 2. Run Marked on the text.
 * 3. Replace placeholders with KaTeX-rendered HTML.
 * This avoids Marked escaping backslashes or changing $ in the math.
 */
function extractMathReplacements(text) {
    const replacements = [];
    let index = 0;

    function placeholder(isBlock) {
        const key = MATH_PLACEHOLDER_PREFIX + (replacements.length) + MATH_PLACEHOLDER_SUFFIX;
        replacements.push({ key, isBlock, math: null });
        return key;
    }

    // Block: $$ ... $$ (do not match \$ \$ — escaped currency)
    let out = text.replace(/(?<!\\)\$\$([\s\S]*?)(?<!\\)\$\$/g, (_, math) => {
        const key = placeholder(true);
        replacements[replacements.length - 1].math = math.trim();
        return key;
    });

    // Display: \[ ... \]
    out = out.replace(/\\\[([\s\S]*?)\\\]/g, (_, math) => {
        const key = placeholder(true);
        replacements[replacements.length - 1].math = math.trim();
        return key;
    });

    // Inline: \( ... \)
    out = out.replace(/\\\(([\s\S]*?)\\\)/g, (_, math) => {
        const key = placeholder(false);
        replacements[replacements.length - 1].math = math.trim();
        return key;
    });

    // Inline: $ ... $ (single $, not $$; do not match \$ — escaped currency)
    out = out.replace(/(?<!\\)\$(?!\$)([^\$\n]+?)(?<!\\)\$/g, (_, math) => {
        const key = placeholder(false);
        replacements[replacements.length - 1].math = math.trim();
        return key;
    });

    return { text: out, replacements };
}

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

/**
 * Replace placeholders in HTML with KaTeX output.
 */
function replacePlaceholdersWithKaTeX(html, replacements) {
    const katex = getKaTeX();
    if (!katex || typeof katex.renderToString !== "function") {
        return replacements.reduce((h, r) => h.replace(r.key, escapeHtml(r.math || "")), html);
    }
    return replacements.reduce((h, r) => {
        const math = r.math || "";
        if (!math) return h.replace(r.key, "");
        try {
            const rendered = katex.renderToString(math, {
                displayMode: r.isBlock,
                throwOnError: false,
            });
            return h.replace(r.key, rendered);
        } catch (e) {
            return h.replace(r.key, `<span class="katex-error">${escapeHtml(math)}</span>`);
        }
    }, html);
}

/**
 * Render markdown and LaTeX from raw text.
 * Math is extracted before Marked so delimiters are not altered, then re-injected after.
 * Currency dollars ($ followed by digit) are escaped before parsing so they render as literal $.
 */
function renderMarkdownLatex(text) {
    if (!text) return "";
    const safeText = escapeCurrencyDollars(text);
    const { text: textWithPlaceholders, replacements } = extractMathReplacements(safeText);
    let html = textWithPlaceholders;
    const markedLib = window.marked?.default ?? window.marked;
    if (markedLib && typeof markedLib.parse === "function") {
        try {
            const parsed = markedLib.parse(textWithPlaceholders);
            html = typeof parsed === "string" ? parsed : String(parsed);
        } catch (e) {
            html = textWithPlaceholders.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
        }
    } else {
        html = textWithPlaceholders.replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br/>");
    }
    return replacePlaceholdersWithKaTeX(html, replacements);
}

export class MarkdownLatexField extends Component {
    static template = "preference_ranking.MarkdownLatexField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    get copyLabel() {
        return _t("Copy to clipboard");
    }

    setup() {
        this.textareaRef = useRef("textarea");
        this.state = useState({ renderedHtml: "", libsReady: false, copied: false });
        this._copyFeedbackTimeout = null;

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
                    this.state.renderedHtml = renderMarkdownLatex(raw);
                }
            });
            this._loadLibsAndRender();
        }
    }

    async _loadLibsAndRender() {
        const raw = formatText(this.props.record.data[this.props.name]);
        if (!raw) {
            this.state.renderedHtml = "";
            this.state.libsReady = true;
            return;
        }
        try {
            // Load KaTeX first (CSS + JS) so math is ready when we render
            loadCSS(KATEX_CSS_CDN);
            await loadScript(KATEX_JS_CDN);
            await loadScript(MARKED_CDN);
        } catch (e) {
            console.warn("Markdown/KaTeX libs failed to load:", e);
        }
        this.state.renderedHtml = renderMarkdownLatex(raw);
        this.state.libsReady = true;
    }

    get renderedMarkup() {
        if (!this.state.libsReady || !this.state.renderedHtml) {
            return markup("");
        }
        return markup(this.state.renderedHtml);
    }

    get rawTextToCopy() {
        if (!this.props.record || !this.props.name) return "";
        return formatText(this.props.record.data[this.props.name] ?? "") || "";
    }

    async onCopy() {
        const text = this.rawTextToCopy;
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            this.state.copied = true;
            if (this._copyFeedbackTimeout) clearTimeout(this._copyFeedbackTimeout);
            this._copyFeedbackTimeout = setTimeout(() => {
                this.state.copied = false;
                this._copyFeedbackTimeout = null;
            }, 2000);
        } catch (e) {
            console.warn("Copy failed:", e);
        }
    }
}

export const markdownLatexField = {
    component: MarkdownLatexField,
    displayName: _t("Markdown & LaTeX"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("markdown_latex", markdownLatexField);
