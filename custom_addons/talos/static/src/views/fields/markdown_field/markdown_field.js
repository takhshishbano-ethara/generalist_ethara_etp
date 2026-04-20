/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useState } from "@odoo/owl";

/* ── Inline Markdown-to-HTML converter ────────────────────────── */

function escapeHtml(s) {
    return s
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function parseMarkdown(src) {
    if (!src) return "";

    let html = "";
    const lines = src.split("\n");
    let i = 0;
    let inList = false;
    let listType = "";

    function closePendingList() {
        if (inList) {
            html += listType === "ol" ? "</ol>\n" : "</ul>\n";
            inList = false;
            listType = "";
        }
    }

    while (i < lines.length) {
        const line = lines[i];

        // ── Fenced code blocks ───
        if (line.trimStart().startsWith("```")) {
            closePendingList();
            const lang = line.trimStart().slice(3).trim();
            const codeLines = [];
            i++;
            while (i < lines.length && !lines[i].trimStart().startsWith("```")) {
                codeLines.push(lines[i]);
                i++;
            }
            i++; // skip closing ```
            const code = escapeHtml(codeLines.join("\n"));
            html += `<pre><code${lang ? ` class="language-${escapeHtml(lang)}"` : ""}>${code}</code></pre>\n`;
            continue;
        }

        // ── Blank line ───
        if (line.trim() === "") {
            closePendingList();
            i++;
            continue;
        }

        // ── Headings ───
        const headingMatch = line.match(/^(#{1,6})\s+(.*)/);
        if (headingMatch) {
            closePendingList();
            const level = headingMatch[1].length;
            const text = inlineFormat(headingMatch[2]);
            html += `<h${level}>${text}</h${level}>\n`;
            i++;
            continue;
        }

        // ── Horizontal rule ───
        if (/^(-{3,}|_{3,}|\*{3,})\s*$/.test(line.trim())) {
            closePendingList();
            html += "<hr/>\n";
            i++;
            continue;
        }

        // ── Blockquote ───
        if (line.trimStart().startsWith("> ")) {
            closePendingList();
            const quoteLines = [];
            while (i < lines.length && lines[i].trimStart().startsWith("> ")) {
                quoteLines.push(lines[i].trimStart().slice(2));
                i++;
            }
            html += `<blockquote>${inlineFormat(quoteLines.join("\n"))}</blockquote>\n`;
            continue;
        }

        // ── Unordered list ───
        const ulMatch = line.match(/^(\s*)[-*+]\s+(.*)/);
        if (ulMatch) {
            if (!inList || listType !== "ul") {
                closePendingList();
                html += "<ul>\n";
                inList = true;
                listType = "ul";
            }
            html += `<li>${inlineFormat(ulMatch[2])}</li>\n`;
            i++;
            continue;
        }

        // ── Ordered list ───
        const olMatch = line.match(/^(\s*)\d+[.)]\s+(.*)/);
        if (olMatch) {
            if (!inList || listType !== "ol") {
                closePendingList();
                html += "<ol>\n";
                inList = true;
                listType = "ol";
            }
            html += `<li>${inlineFormat(olMatch[2])}</li>\n`;
            i++;
            continue;
        }

        // ── Table ───
        if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:]*-+[\s:]*/.test(lines[i + 1])) {
            closePendingList();
            html += parseTable(lines, i);
            // skip header + separator + body rows
            i++; // header
            i++; // separator
            while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
                i++;
            }
            continue;
        }

        // ── Paragraph ───
        closePendingList();
        const paraLines = [];
        while (
            i < lines.length &&
            lines[i].trim() !== "" &&
            !lines[i].trimStart().startsWith("```") &&
            !lines[i].match(/^#{1,6}\s/) &&
            !lines[i].match(/^(\s*)[-*+]\s/) &&
            !lines[i].match(/^(\s*)\d+[.)]\s/) &&
            !lines[i].trimStart().startsWith("> ")
        ) {
            paraLines.push(lines[i]);
            i++;
        }
        html += `<p>${inlineFormat(paraLines.join("\n"))}</p>\n`;
    }

    closePendingList();
    return html;
}

function parseTable(lines, startIdx) {
    const headerCells = splitTableRow(lines[startIdx]);
    let tableHtml = "<table><thead><tr>";
    for (const cell of headerCells) {
        tableHtml += `<th>${inlineFormat(cell.trim())}</th>`;
    }
    tableHtml += "</tr></thead><tbody>";

    let j = startIdx + 2; // skip separator
    while (j < lines.length && lines[j].includes("|") && lines[j].trim() !== "") {
        const cells = splitTableRow(lines[j]);
        tableHtml += "<tr>";
        for (let c = 0; c < headerCells.length; c++) {
            tableHtml += `<td>${inlineFormat((cells[c] || "").trim())}</td>`;
        }
        tableHtml += "</tr>";
        j++;
    }
    tableHtml += "</tbody></table>\n";
    return tableHtml;
}

function splitTableRow(row) {
    let trimmed = row.trim();
    if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
    if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
    return trimmed.split("|");
}

function inlineFormat(text) {
    if (!text) return "";
    let s = escapeHtml(text);
    // Inline code (backtick) — must come first before other inline formatting
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Images
    s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, '<img src="$2" alt="$1"/>');
    // Links
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    // Bold + italic
    s = s.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
    s = s.replace(/___(.+?)___/g, "<strong><em>$1</em></strong>");
    // Bold
    s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/__(.+?)__/g, "<strong>$1</strong>");
    // Italic
    s = s.replace(/\*(.+?)\*/g, "<em>$1</em>");
    s = s.replace(/_(.+?)_/g, "<em>$1</em>");
    // Strikethrough
    s = s.replace(/~~(.+?)~~/g, "<del>$1</del>");
    // Line breaks within paragraphs
    s = s.replace(/\n/g, "<br/>");
    return s;
}

/* ── OWL Component ────────────────────────────────────────────── */

export class TalosMarkdownField extends Component {
    static template = "talos.MarkdownField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({ renderedHtml: "" });

        useRecordObserver((record) => {
            const raw = record.data[this.props.name];
            this.state.renderedHtml = parseMarkdown(raw || "");
        });

        // Initial render
        const raw = this.props.record.data[this.props.name];
        this.state.renderedHtml = parseMarkdown(raw || "");
    }

    get renderedMarkup() {
        return markup(this.state.renderedHtml || "");
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
