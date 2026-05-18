/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, markup, useRef, useState, onPatched, onWillUnmount } from "@odoo/owl";

function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
}

function syntaxHighlightJson(jsonStr) {
    return escapeHtml(jsonStr)
        .replace(/("(?:\\.|[^"\\])*")\s*:/g, '<span class="json-key">$1</span>:')
        .replace(/:\s*("(?:\\.|[^"\\])*")/g, ': <span class="json-string">$1</span>')
        .replace(/:\s*(\d+(?:\.\d+)?)/g, ': <span class="json-number">$1</span>')
        .replace(/:\s*(true|false)/g, ': <span class="json-boolean">$1</span>')
        .replace(/:\s*(null)/g, ': <span class="json-null">$1</span>');
}

function stripCodeFences(text) {
    let s = (text || "").trim();
    if (s.startsWith("```")) {
        const nl = s.indexOf("\n");
        if (nl !== -1) s = s.slice(nl + 1);
        if (s.endsWith("```")) s = s.slice(0, -3).trimEnd();
    }
    return s;
}

export class SkollPromptField extends Component {
    static template = "skoll.PromptField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            generating: false,
            streaming: false,
            streamText: "",
            truncated: false,
            hasClaudeTrajectory: false,
            goldenHtml: null,
            goldenPretty: "",
            copied: false,
        });
        this.notification = useService("notification");
        this._abortController = null;
        this._streamBodyRef = useRef("streamBody");

        this._onExtStreamStart = () => this._handleExtStreamStart();
        this._onExtStreamChunk = (ev) => this._handleExtStreamChunk(ev.detail);
        this._onExtStreamEnd = (ev) => this._handleExtStreamEnd(ev.detail);
        this.env.bus.addEventListener("SKOLL_STREAM_START", this._onExtStreamStart);
        this.env.bus.addEventListener("SKOLL_STREAM_CHUNK", this._onExtStreamChunk);
        this.env.bus.addEventListener("SKOLL_STREAM_END", this._onExtStreamEnd);

        useRecordObserver((record) => {
            this.state.hasClaudeTrajectory = !!(record.data.claude_trajectory || "").trim();
            if (!this.state.streaming) {
                this._updateGoldenContent(record.data.golden_trajectory);
            } else if (!this.state.generating) {
                // External stream (improve) completed — record was reloaded
                this.state.streaming = false;
                this.state.streamText = "";
                this._updateGoldenContent(record.data.golden_trajectory);
            }
        });
        this.state.hasClaudeTrajectory = !!(this.props.record.data.claude_trajectory || "").trim();
        this._updateGoldenContent(this.props.record.data.golden_trajectory);

        onPatched(() => {
            if (this.state.streaming) {
                requestAnimationFrame(() => this._scrollStreamToBottom());
            }
        });

        onWillUnmount(() => {
            this._abortStream();
            this.env.bus.removeEventListener("SKOLL_STREAM_START", this._onExtStreamStart);
            this.env.bus.removeEventListener("SKOLL_STREAM_CHUNK", this._onExtStreamChunk);
            this.env.bus.removeEventListener("SKOLL_STREAM_END", this._onExtStreamEnd);
        });
    }

    _abortStream() {
        if (this._abortController) {
            this._abortController.abort();
            this._abortController = null;
        }
    }

    _updateGoldenContent(raw) {
        if (!raw || !raw.trim()) {
            this.state.goldenHtml = null;
            this.state.goldenPretty = "";
            return;
        }
        const stripped = stripCodeFences(raw);
        let parsed;
        try {
            parsed = JSON.parse(stripped);
        } catch (_e) {
            this.state.goldenPretty = raw;
            this.state.goldenHtml = markup(
                `<pre class="skoll-json-block"><code>${escapeHtml(raw)}</code></pre>`
            );
            return;
        }
        const pretty = JSON.stringify(parsed, null, 2);
        this.state.goldenPretty = pretty;
        this.state.goldenHtml = markup(
            `<pre class="skoll-json-block"><code>${syntaxHighlightJson(pretty)}</code></pre>`
        );
    }

    _scrollStreamToBottom() {
        const wrapper = this._streamBodyRef.el;
        if (!wrapper || !this.state.streaming) return;
        const pre = wrapper.querySelector(".skoll-json-block") || wrapper;
        pre.scrollTop = pre.scrollHeight;
    }

    _handleExtStreamStart() {
        if (this.state.generating) return;
        this.state.streaming = true;
        this.state.streamText = "";
        this.state.goldenHtml = null;
        this.state.goldenPretty = "";
        this.state.truncated = false;
    }

    _handleExtStreamChunk(data) {
        if (this.state.generating || !data) return;
        this.state.streamText = data.text || "";
    }

    _handleExtStreamEnd(data) {
        if (this.state.generating) return;
        this.state.streaming = false;
        this.state.truncated = !!data?.truncated;
        const finalText = data?.text || this.state.streamText;
        this.state.streamText = "";
        if (finalText) {
            this._updateGoldenContent(finalText);
        }
    }

    get isReadonly() {
        return this.props.readonly;
    }

    get isAiMode() {
        return this.props.record.data.golden_input_mode === "ai";
    }

    get hasClaudeTrajectory() {
        return this.state.hasClaudeTrajectory;
    }

    get hasGoldenContent() {
        return !!this.state.goldenHtml;
    }

    get canGenerate() {
        return this.isAiMode && !this.isReadonly && this.hasClaudeTrajectory && !this.state.streaming;
    }

    get streamHighlightedHtml() {
        if (!this.state.streamText) return null;
        const escaped = escapeHtml(this.state.streamText);
        return markup(
            `<pre class="skoll-json-block skoll-json-streaming"><code>${escaped}</code><span class="skoll-stream-cursor">\u2588</span></pre>`
        );
    }

    async onGenerate() {
        if (!this.canGenerate || this.state.generating) return;

        this._abortController = new AbortController();
        this.state.generating = true;
        this.state.streaming = true;
        this.state.streamText = "";
        this.state.truncated = false;
        this.state.goldenHtml = null;
        this.state.goldenPretty = "";

        let accumulated = "";
        let stopReason = "end_turn";

        try {
            const resp = await fetch("/skoll/golden/generate_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    record_id: this.props.record.resId,
                }),
                credentials: "same-origin",
                signal: this._abortController.signal,
            });

            if (!resp.ok) {
                const errText = await resp.text();
                throw new Error(`HTTP ${resp.status}: ${errText.substring(0, 300)}`);
            }

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let sseBuffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                sseBuffer += decoder.decode(value, { stream: true });

                const lines = sseBuffer.split("\n");
                sseBuffer = lines.pop();

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    const jsonStr = line.slice(6);
                    if (jsonStr === "[DONE]") continue;

                    let event;
                    try {
                        event = JSON.parse(jsonStr);
                    } catch {
                        continue;
                    }

                    if (event.type === "delta" && event.text) {
                        accumulated += event.text;
                        this.state.streamText = accumulated;
                    } else if (event.type === "stop") {
                        stopReason = event.stopReason || "end_turn";
                    } else if (event.type === "error") {
                        throw new Error(event.message || "Stream error");
                    }
                }
            }

            if (sseBuffer.startsWith("data: ")) {
                try {
                    const event = JSON.parse(sseBuffer.slice(6));
                    if (event.type === "delta" && event.text) {
                        accumulated += event.text;
                        this.state.streamText = accumulated;
                    } else if (event.type === "error") {
                        throw new Error(event.message || "Stream error");
                    }
                } catch {}
            }

            if (accumulated) {
                let cleaned = accumulated.trim();
                if (cleaned.startsWith("```")) {
                    const nl = cleaned.indexOf("\n");
                    if (nl !== -1) cleaned = cleaned.slice(nl + 1);
                    if (cleaned.endsWith("```")) cleaned = cleaned.slice(0, -3).trimEnd();
                }
                await this.props.record.update({ golden_trajectory: cleaned });
                await this.props.record.save();
                if (stopReason === "max_tokens") {
                    this.state.truncated = true;
                    this.notification.add(
                        _t("Output truncated — the model hit the max token limit. The trajectory JSON is likely incomplete."),
                        { type: "warning", sticky: true },
                    );
                } else {
                    this.notification.add(_t("Golden trajectory generated from Claude 4.7 trajectory"), { type: "success" });
                }
                await Promise.all([this._buildSpawnTree(), this._runSchemaValidation()]);
            }
        } catch (e) {
            if (e.name !== "AbortError") {
                this.notification.add(
                    _t("Generation failed: ") + (e.message || String(e)),
                    { type: "danger" },
                );
            }
        } finally {
            this.state.generating = false;
            this.state.streaming = false;
            this.state.streamText = "";
            this._abortController = null;
            this._updateGoldenContent(this.props.record.data.golden_trajectory);
        }
    }

    onStopGenerate() {
        this._abortStream();
    }

    async onCopyJson() {
        const text = this.state.goldenPretty || this.props.record.data.golden_trajectory || "";
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

    async _buildSpawnTree() {
        const recordId = this.props.record.resId;
        if (!recordId) return;
        try {
            const result = await rpc("/skoll/golden/spawn_tree", { record_id: recordId });
            if (result.status === "success") {
                this.env.bus.trigger("SKOLL_SPAWN_TREE_READY", { spawn_tree: result.spawn_tree });
                await this.props.record.load();
            }
        } catch (_e) {}
    }

    async _runSchemaValidation() {
        const recordId = this.props.record.resId;
        if (!recordId) return;
        try {
            const result = await rpc("/skoll/golden/validate", { record_id: recordId });
            if (result.status === "success") {
                if (result.valid) {
                    this.notification.add(_t("Schema validation passed"), { type: "success" });
                } else {
                    this.notification.add(
                        _t("Schema validation: %s error(s), %s warning(s)", result.error_count, result.warning_count),
                        { type: "warning", sticky: true },
                    );
                }
                await this.props.record.load();
            }
        } catch (_e) {}
    }
}

export const skollPromptField = {
    component: SkollPromptField,
    displayName: _t("Prompt Editor"),
    supportedTypes: ["text"],
    extractProps: ({ attrs }) => ({
        placeholder: attrs.placeholder,
    }),
};

registry.category("fields").add("skoll_prompt", skollPromptField);
