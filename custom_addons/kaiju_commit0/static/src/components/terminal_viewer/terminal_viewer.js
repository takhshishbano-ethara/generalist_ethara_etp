/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onWillUnmount, useState } from "@odoo/owl";

export class Commit0Terminal extends Component {
    static template = "kaiju_commit0.Commit0Terminal";
    static props = { ...standardFieldProps };

    setup() {
        this._copyTimeout = null;

        this.state = useState({
            copied: false,
            darkTheme: true,
        });

        onWillUnmount(() => {
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
        });
    }

    get logText() {
        return this.props.record.data[this.props.name] || "";
    }

    get lines() {
        if (!this.logText) return [];
        return this.logText.split("\n");
    }

    get isEmpty() {
        return !this.logText;
    }

    get themeClass() {
        return this.state.darkTheme ? "c0-term-dark" : "c0-term-light";
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
