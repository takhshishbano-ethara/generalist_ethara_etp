/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, useRef, useState, onMounted, onPatched, onWillUnmount } from "@odoo/owl";

export class SkollPromptField extends Component {
    static template = "skoll.PromptField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            value: "",
            generating: false,
            truncated: false,
        });
        this.notification = useService("notification");
        this.textareaRef = useRef("promptTextarea");
        this._abortController = null;

        useRecordObserver((record) => {
            this.state.value = record.data[this.props.name] || "";
        });
        this.state.value = this.props.record.data[this.props.name] || "";

        onMounted(() => this._autoResize());
        onPatched(() => this._autoResize());
        onWillUnmount(() => this._abortStream());
    }

    _autoResize() {
        const el = this.textareaRef.el;
        if (el) {
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 400) + "px";
        }
    }

    _abortStream() {
        if (this._abortController) {
            this._abortController.abort();
            this._abortController = null;
        }
    }

    get isReadonly() {
        return this.props.readonly;
    }

    get isAiMode() {
        return this.props.record.data.mode === "ai";
    }

    get canGenerate() {
        return this.isAiMode && !this.isReadonly;
    }

    onInput(ev) {
        this.state.value = ev.target.value;
        this.props.record.update({ [this.props.name]: ev.target.value });
        this._autoResize();
    }

    async onGenerate() {
        if (!this.canGenerate || this.state.generating) return;

        this._abortController = new AbortController();
        this.state.generating = true;
        this.state.truncated = false;
        this.env.bus.trigger("SKOLL_STREAM_START");

        let accumulated = "";
        let stopReason = "end_turn";

        try {
            const resp = await fetch("/skoll/generate_stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt: this.state.value,
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
                        this.env.bus.trigger("SKOLL_STREAM_CHUNK", { text: accumulated });
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
                    } else if (event.type === "error") {
                        throw new Error(event.message || "Stream error");
                    }
                } catch {}
            }

            if (accumulated) {
                await this.props.record.update({ content: accumulated });
                await this.props.record.save();
                if (stopReason === "max_tokens") {
                    this.state.truncated = true;
                    this.notification.add(
                        _t("Output truncated — the model hit the max token limit. The trajectory JSON is likely incomplete."),
                        { type: "warning", sticky: true },
                    );
                } else {
                    this.notification.add(_t("Content generated successfully"), { type: "success" });
                }
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
            this._abortController = null;
            this.env.bus.trigger("SKOLL_STREAM_END", { text: accumulated, truncated: stopReason === "max_tokens" });
        }
    }

    onStopGenerate() {
        this._abortStream();
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
