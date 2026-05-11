/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";

const REVEAL_INTERVAL = 80;

export class Commit0Terminal extends Component {
    static template = "kaiju_commit0.Commit0Terminal";
    static props = { ...standardFieldProps };

    setup() {
        this._revealTimer = null;
        this._copyTimeout = null;
        this.containerRef = useRef("termContainer");

        this.state = useState({
            visibleLines: 0,
            copied: false,
            darkTheme: true,
        });

        onMounted(() => {
            this._revealAll();
        });

        onWillUnmount(() => {
            if (this._revealTimer) clearInterval(this._revealTimer);
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
        });
    }

    get logText() {
        return this.props.record.data[this.props.name] || "";
    }

    get allLines() {
        if (!this.logText) return [];
        return this.logText.split("\n");
    }

    get lines() {
        return this.allLines.slice(0, this.state.visibleLines);
    }

    get isEmpty() {
        return !this.logText;
    }

    get themeClass() {
        return this.state.darkTheme ? "c0-term-dark" : "c0-term-light";
    }

    get isRevealing() {
        return this.state.visibleLines < this.allLines.length;
    }

    lineClass(line) {
        const upper = line.toUpperCase();
        if (upper.includes("ERROR") || upper.includes("FATAL") || upper.includes("FAIL")) {
            return "c0-line-error";
        }
        if (upper.includes("WARN")) {
            return "c0-line-warn";
        }
        if (upper.includes("SUCCESS") || upper.includes("COMPLETE") || upper.includes("PASS")) {
            return "c0-line-success";
        }
        if (upper.includes("STEP ") || upper.includes("===")) {
            return "c0-line-step";
        }
        return "";
    }

    _revealAll() {
        const total = this.allLines.length;
        if (total === 0) {
            this.state.visibleLines = 0;
            return;
        }
        this.state.visibleLines = 0;
        this._revealTimer = setInterval(() => {
            this.state.visibleLines++;
            this._scrollToBottom();
            if (this.state.visibleLines >= total) {
                clearInterval(this._revealTimer);
                this._revealTimer = null;
            }
        }, REVEAL_INTERVAL);
    }

    _scrollToBottom() {
        const el = this.containerRef.el;
        if (el) {
            requestAnimationFrame(() => {
                el.scrollTop = el.scrollHeight;
            });
        }
    }

    async onCopy() {
        if (!this.logText) return;
        try {
            await navigator.clipboard.writeText(this.logText);
            this.state.copied = true;
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
            this._copyTimeout = setTimeout(() => {
                this.state.copied = false;
            }, 2000);
        } catch {}
    }

    toggleTheme() {
        this.state.darkTheme = !this.state.darkTheme;
    }
}

export const commit0TerminalField = {
    component: Commit0Terminal,
    displayName: "Terminal Viewer",
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("commit0_terminal", commit0TerminalField);
