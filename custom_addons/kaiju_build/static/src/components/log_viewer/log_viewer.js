/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, onWillUpdateProps, useRef, useState } from "@odoo/owl";

const POLL_STATUSES = new Set(["queued", "building"]);
const POLL_INTERVAL = 3000;

export class KaijuLogViewer extends Component {
    static template = "kaiju_build.KaijuLogViewer";
    static props = { ...standardFieldProps };

    setup() {
        this._interval = null;
        this._polling = false;
        this._reloading = false;
        this._copyTimeout = null;
        this._userScrolledUp = false;

        this.logContainerRef = useRef("logContainer");

        this.state = useState({
            darkTheme: true,
            copied: false,
            autoScroll: true,
            lineCount: 0,
            refreshing: false,
        });

        onMounted(() => {
            this._updateLineCount();
            this._scrollToBottom();
            this._attachScrollListener();
            this._checkAndPoll();
        });

        onWillUpdateProps(() => {
            this._updateLineCount();
            this._scheduleScroll();
            this._checkAndPoll();
        });

        onWillUnmount(() => {
            this._stopPoll();
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
        });
    }

    // ── Getters ──────────────────────────────────────────────────────

    get logText() {
        return this.props.record.data[this.props.name] || "";
    }

    get lines() {
        const text = this.logText;
        if (!text) return [];
        return text.split("\n");
    }

    get buildStatus() {
        return this.props.record.data.status || "draft";
    }

    get isActive() {
        return POLL_STATUSES.has(this.buildStatus);
    }

    get isEmpty() {
        return !this.logText;
    }

    get themeClass() {
        return this.state.darkTheme ? "kj-log-dark" : "kj-log-light";
    }

    // ── Line classification (keyword highlighting) ───────────────────

    lineClass(line) {
        const upper = line.toUpperCase();
        if (upper.includes("ERROR") || upper.includes("FATAL") || upper.includes("TRACEBACK")) {
            return "kj-log-line-error";
        }
        if (upper.includes("WARN")) {
            return "kj-log-line-warn";
        }
        if (upper.includes("SUCCESS") || upper.includes("COMPLETE") || upper.includes("DONE")) {
            return "kj-log-line-success";
        }
        if (upper.startsWith("STEP ") || upper.startsWith("===") || upper.startsWith("---")) {
            return "kj-log-line-step";
        }
        return "";
    }

    // ── Scrolling ────────────────────────────────────────────────────

    _attachScrollListener() {
        const el = this.logContainerRef.el;
        if (!el) return;
        el.addEventListener("scroll", this._onScroll.bind(this));
    }

    _onScroll() {
        const el = this.logContainerRef.el;
        if (!el) return;
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        this.state.autoScroll = atBottom;
    }

    _scrollToBottom() {
        if (!this.state.autoScroll) return;
        const el = this.logContainerRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }

    _scheduleScroll() {
        requestAnimationFrame(() => {
            this._updateLineCount();
            this._scrollToBottom();
        });
    }

    scrollToTop() {
        const el = this.logContainerRef.el;
        if (el) {
            el.scrollTop = 0;
            this.state.autoScroll = false;
        }
    }

    scrollToBottomClick() {
        this.state.autoScroll = true;
        const el = this.logContainerRef.el;
        if (el) {
            el.scrollTop = el.scrollHeight;
        }
    }

    // ── Copy ─────────────────────────────────────────────────────────

    async onCopy() {
        if (!this.logText) return;
        try {
            await navigator.clipboard.writeText(this.logText);
            this.state.copied = true;
            if (this._copyTimeout) clearTimeout(this._copyTimeout);
            this._copyTimeout = setTimeout(() => {
                this.state.copied = false;
                this._copyTimeout = null;
            }, 2000);
        } catch {
        }
    }

    // ── Theme ────────────────────────────────────────────────────────

    toggleTheme() {
        this.state.darkTheme = !this.state.darkTheme;
    }

    // ── Auto-refresh polling ─────────────────────────────────────────

    _shouldPoll() {
        return POLL_STATUSES.has(this.buildStatus);
    }

    _checkAndPoll() {
        if (this._shouldPoll()) {
            this._startPoll();
        } else {
            this._stopPoll();
        }
    }

    _startPoll() {
        if (this._polling) return;
        this._polling = true;
        this.state.refreshing = true;
        this._interval = setInterval(() => this._poll(), POLL_INTERVAL);
    }

    async _poll() {
        if (this._reloading) return;
        this._reloading = true;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
        } catch {
            this._stopPoll();
        } finally {
            this._reloading = false;
            this._checkAndPoll();
        }
    }

    _stopPoll() {
        this._polling = false;
        this.state.refreshing = false;
        if (this._interval) {
            clearInterval(this._interval);
            this._interval = null;
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────

    _updateLineCount() {
        this.state.lineCount = this.lines.length;
    }
}

export const kaijuLogViewerField = {
    component: KaijuLogViewer,
    displayName: "Build Log Viewer",
    supportedTypes: ["text", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("kaiju_log_viewer", kaijuLogViewerField);
