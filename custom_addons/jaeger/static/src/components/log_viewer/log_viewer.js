/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onPatched, onWillUnmount, useRef, useState } from "@odoo/owl";

const POLL_STATUSES = new Set([
    "running", "building", "queued", "generating",
    "dispatched", "evaluating", "converting",
]);
const POLL_INTERVAL = 3000;
const MAX_CONSECUTIVE_FAILURES = 5;

export class JaegerLogViewer extends Component {
    static template = "jaeger.JaegerLogViewer";
    static props = { ...standardFieldProps };

    setup() {
        this._interval = null;
        this._polling = false;
        this._reloading = false;
        this._copyTimeout = null;
        this._failCount = 0;

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

        onPatched(() => {
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

    get collectionStatus() {
        return this.props.record.data.pr_collection_status || "pending";
    }

    get isActive() {
        return POLL_STATUSES.has(this.collectionStatus);
    }

    get isEmpty() {
        return !this.logText;
    }

    get themeClass() {
        return this.state.darkTheme ? "jgr-log-dark" : "jgr-log-light";
    }

    // ── Line classification (keyword highlighting) ───────────────────

    lineClass(line) {
        const upper = line.toUpperCase();
        if (upper.includes("ERROR") || upper.includes("FATAL") || upper.includes("TRACEBACK")) {
            return "jgr-log-line-error";
        }
        if (upper.includes("WARN")) {
            return "jgr-log-line-warn";
        }
        if (upper.includes("SUCCESS") || upper.includes("COMPLETE") || upper.includes("DONE")) {
            return "jgr-log-line-success";
        }
        if (upper.startsWith("STEP ") || upper.startsWith("===") || upper.startsWith("---") || upper.includes("STEP ")) {
            return "jgr-log-line-step";
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
        return POLL_STATUSES.has(this.collectionStatus);
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
        this._failCount = 0;
        this.state.refreshing = true;
        this._interval = setInterval(() => this._poll(), POLL_INTERVAL);
    }

    async _poll() {
        if (this._reloading) return;
        this._reloading = true;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
            this._failCount = 0;
        } catch (e) {
            this._failCount++;
            if (this._failCount >= MAX_CONSECUTIVE_FAILURES) {
                this._stopPoll();
                return;
            }
        } finally {
            this._reloading = false;
            if (this._polling) {
                this._checkAndPoll();
            }
        }
    }

    _stopPoll() {
        this._polling = false;
        this._failCount = 0;
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

export const jaegerLogViewerField = {
    component: JaegerLogViewer,
    displayName: "Pipeline Log Viewer",
    supportedTypes: ["text", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("jaeger_log_viewer", jaegerLogViewerField);
