/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useRef, useState, onMounted, onPatched, onWillUnmount } from "@odoo/owl";

const JSON_HIGHLIGHT_CDN =
    "https://cdn.jsdelivr.net/npm/json-format-highlight@1.0.4/dist/json-format-highlight.min.js";

const HIGHLIGHT_COLORS = {
    keyColor: "#89b4fa",
    stringColor: "#a6e3a1",
    numberColor: "#fab387",
    trueColor: "#cba6f7",
    falseColor: "#cba6f7",
    nullColor: "#f38ba8",
};

let _highlighterReady = null;

function _loadHighlighter() {
    if (_highlighterReady) return _highlighterReady;
    if (window.jsonFormatHighlight) {
        _highlighterReady = Promise.resolve();
        return _highlighterReady;
    }
    _highlighterReady = new Promise((resolve, reject) => {
        const s = document.createElement("script");
        s.src = JSON_HIGHLIGHT_CDN;
        s.onload = resolve;
        s.onerror = () => reject(new Error("Failed to load json-format-highlight"));
        document.head.appendChild(s);
    });
    return _highlighterReady;
}

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

function highlightJson(parsed) {
    if (window.jsonFormatHighlight) {
        return window.jsonFormatHighlight(parsed, HIGHLIGHT_COLORS);
    }
    return escapeHtml(JSON.stringify(parsed, null, 2));
}

function formatJsonContent(raw) {
    if (!raw || !raw.trim()) return null;
    try {
        return JSON.parse(raw.trim());
    } catch (_e) {
        return null;
    }
}

export class SkollJsonField extends Component {
    static template = "skoll.JsonField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            prettyJson: "",
            highlightedHtml: null,
            streaming: false,
            streamText: "",
            qcRunning: false,
            improving: false,
            truncated: false,
            aiEditing: false,
            aiEditBuffer: "",
            copied: false,
        });
        this.editTextareaRef = useRef("editTextarea");
        this._streamBodyRef = useRef("streamBody");
        this._qcAbortController = null;
        this._improveAbortController = null;

        this._onStreamStart = () => this._handleStreamStart();
        this._onStreamChunk = (ev) => this._handleStreamChunk(ev.detail);
        this._onStreamEnd = (ev) => this._handleStreamEnd(ev.detail);

        this.env.bus.addEventListener("SKOLL_STREAM_START", this._onStreamStart);
        this.env.bus.addEventListener("SKOLL_STREAM_CHUNK", this._onStreamChunk);
        this.env.bus.addEventListener("SKOLL_STREAM_END", this._onStreamEnd);

        _loadHighlighter().catch(() => {});

        useRecordObserver((record) => {
            if (!this.state.streaming) {
                this._updateContent(record.data[this.props.name]);
            }
        });
        this._updateContent(this.props.record.data[this.props.name]);

        onMounted(() => this._autoResizeEditTextarea());
        onPatched(() => {
            this._autoResizeEditTextarea();
            if (this.state.streaming) {
                requestAnimationFrame(() => this._scrollStreamToBottom());
            }
        });
        onWillUnmount(() => {
            this.env.bus.removeEventListener("SKOLL_STREAM_START", this._onStreamStart);
            this.env.bus.removeEventListener("SKOLL_STREAM_CHUNK", this._onStreamChunk);
            this.env.bus.removeEventListener("SKOLL_STREAM_END", this._onStreamEnd);
            if (this._qcAbortController) this._qcAbortController.abort();
            if (this._improveAbortController) this._improveAbortController.abort();
        });
    }

    _autoResizeEditTextarea() {
        const el = this.editTextareaRef.el;
        if (el) {
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 800) + "px";
        }
    }

    _scrollStreamToBottom() {
        const wrapper = this._streamBodyRef.el;
        if (!wrapper || !this.state.streaming) return;
        const pre = wrapper.querySelector(".skoll-json-block") || wrapper;
        pre.scrollTop = pre.scrollHeight;
    }

    _updateContent(raw) {
        const parsed = formatJsonContent(raw || "");
        if (parsed !== null) {
            this.state.prettyJson = JSON.stringify(parsed, null, 2);
            const highlighted = highlightJson(parsed);
            this.state.highlightedHtml = markup(
                `<pre class="skoll-json-block"><code>${highlighted}</code></pre>`
            );
        } else {
            this.state.prettyJson = raw || "";
            if (raw && raw.trim()) {
                this.state.highlightedHtml = markup(
                    `<pre class="skoll-json-block"><code>${escapeHtml(raw)}</code></pre>`
                );
            } else {
                this.state.highlightedHtml = null;
            }
        }
    }

    _handleStreamStart() {
        this.state.streaming = true;
        this.state.streamText = "";
        this.state.highlightedHtml = null;
        this.state.prettyJson = "";
        this.state.truncated = false;
    }

    _handleStreamChunk(data) {
        if (!data) return;
        this.state.streamText = data.text || "";
    }

    _handleStreamEnd(data) {
        this.state.streaming = false;
        this.state.truncated = !!data?.truncated;
        const finalText = data?.text || this.state.streamText;
        this.state.streamText = "";
        if (finalText) {
            this._updateContent(finalText);
        } else {
            this._updateContent(this.props.record.data[this.props.name]);
        }
    }

    get hasContent() {
        return !!this.state.prettyJson;
    }

    get isReadonly() {
        return this.props.readonly;
    }

    get isAiMode() {
        return this.props.record.data.mode === "ai";
    }

    get isManualEdit() {
        return !this.isReadonly && !this.isAiMode;
    }

    get manualValue() {
        return this.props.record.data[this.props.name] || "";
    }

    get streamHighlightedHtml() {
        if (!this.state.streamText) return null;
        const escaped = escapeHtml(this.state.streamText);
        return markup(
            `<pre class="skoll-json-block skoll-json-streaming"><code>${escaped}</code><span class="skoll-stream-cursor">\u2588</span></pre>`
        );
    }

    onManualInput(ev) {
        this.props.record.update({ [this.props.name]: ev.target.value });
        ev.target.style.height = "auto";
        ev.target.style.height = Math.min(ev.target.scrollHeight, 800) + "px";
    }

    async onCopyJson() {
        const text = this.state.prettyJson || this.props.record.data[this.props.name] || "";
        if (!text) return;
        try {
            await navigator.clipboard.writeText(text);
            this.state.copied = true;
            setTimeout(() => (this.state.copied = false), 2000);
        } catch (_e) {
            const ta = document.createElement("textarea");
            ta.value = text;
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            this.state.copied = true;
            setTimeout(() => (this.state.copied = false), 2000);
        }
    }

    onStartAiEdit() {
        this.state.aiEditing = true;
        this.state.aiEditBuffer = this.state.prettyJson || this.props.record.data[this.props.name] || "";
    }

    onAiEditInput(ev) {
        this.state.aiEditBuffer = ev.target.value;
        ev.target.style.height = "auto";
        ev.target.style.height = Math.min(ev.target.scrollHeight, 800) + "px";
    }

    async onSaveAiEdit() {
        await this.props.record.update({ [this.props.name]: this.state.aiEditBuffer });
        this._updateContent(this.state.aiEditBuffer);
        this.state.aiEditing = false;
        this.state.aiEditBuffer = "";
    }

    onCancelAiEdit() {
        this.state.aiEditing = false;
        this.state.aiEditBuffer = "";
    }

    get canRunQc() {
        return this.hasContent && !this.state.streaming && !this.state.qcRunning;
    }

    get canImprove() {
        const qcStatus = this.props.record.data.qc_status;
        return (
            this.hasContent &&
            !this.state.streaming &&
            !this.state.qcRunning &&
            !this.state.improving &&
            (qcStatus === "fail" || qcStatus === "needs_revision") &&
            !!this.props.record.data.qc_result
        );
    }

    async onRunQc() {
        if (!this.canRunQc) return;

        if (this.props.record.isDirty) {
            await this.props.record.save();
        }

        const recordId = this.props.record.resId;
        if (!recordId) {
            console.warn("QC: record not saved yet");
            return;
        }

        this._qcAbortController = new AbortController();
        this.state.qcRunning = true;

        let accumulated = "";
        try {
            const resp = await fetch("/skoll/qc_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ record_id: recordId }),
                credentials: "same-origin",
                signal: this._qcAbortController.signal,
            });

            if (!resp.ok) {
                const errText = await resp.text();
                console.warn("QC request failed:", resp.status, errText.substring(0, 300));
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });
                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop() || "";
                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const payload = JSON.parse(line.slice(6));
                        if (payload.type === "delta" && payload.text) {
                            accumulated += payload.text;
                        } else if (payload.type === "error") {
                            console.warn("QC stream error:", payload.message);
                        }
                    } catch (_e) {}
                }
            }

            if (accumulated.trim()) {
                await this.props.record.update({ qc_result: accumulated });
                this._updateQcStatus(accumulated);
                await this.props.record.save();
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                console.warn("QC stream failed:", err);
            }
        } finally {
            this.state.qcRunning = false;
            this._qcAbortController = null;
            await this.props.record.load();
        }
    }

    async onImprove() {
        if (!this.canImprove) return;

        if (this.props.record.isDirty) {
            await this.props.record.save();
        }

        const recordId = this.props.record.resId;
        if (!recordId) return;

        this._improveAbortController = new AbortController();
        this.state.improving = true;

        this.env.bus.trigger("SKOLL_STREAM_START");

        let accumulated = "";
        try {
            const resp = await fetch("/skoll/improve_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ record_id: recordId }),
                credentials: "same-origin",
                signal: this._improveAbortController.signal,
            });

            if (!resp.ok) {
                console.warn("Improve request failed:", resp.status);
                return;
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });
                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop() || "";
                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const payload = JSON.parse(line.slice(6));
                        if (payload.type === "delta" && payload.text) {
                            accumulated += payload.text;
                            this.env.bus.trigger("SKOLL_STREAM_CHUNK", { text: accumulated });
                        } else if (payload.type === "stop") {
                            const truncated = payload.stopReason === "max_tokens";
                            this.env.bus.trigger("SKOLL_STREAM_END", { text: accumulated, truncated });
                        }
                    } catch (_e) {}
                }
            }

            if (accumulated.trim()) {
                await this.props.record.update({ content: accumulated });
                await this.props.record.save();
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                console.warn("Improve stream failed:", err);
            }
        } finally {
            this.state.improving = false;
            this._improveAbortController = null;
            await this.props.record.load();
        }
    }

    _updateQcStatus(qcText) {
        try {
            const parsed = JSON.parse(qcText.trim());
            const verdict = (parsed.verdict || "").toLowerCase();
            if (["pass", "fail", "needs_revision"].includes(verdict)) {
                this.props.record.update({ qc_status: verdict });
            }
        } catch (_e) {}
    }
}

export const skollJsonField = {
    component: SkollJsonField,
    displayName: _t("JSON Viewer"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("skoll_json", skollJsonField);
