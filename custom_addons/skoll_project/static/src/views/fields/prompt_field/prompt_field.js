/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { useRecordObserver } from "@web/model/relational_model/utils";

import { Component, useState, onWillUnmount } from "@odoo/owl";

export class SkollPromptField extends Component {
    static template = "skoll.PromptField";
    static props = {
        ...standardFieldProps,
        placeholder: { type: String, optional: true },
    };

    setup() {
        this.state = useState({
            generating: false,
            truncated: false,
            hasClaudeTrajectory: false,
        });
        this.notification = useService("notification");
        this._abortController = null;

        useRecordObserver((record) => {
            this.state.hasClaudeTrajectory = !!(record.data.claude_trajectory || "").trim();
        });
        this.state.hasClaudeTrajectory = !!(this.props.record.data.claude_trajectory || "").trim();

        onWillUnmount(() => this._abortStream());
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
        return this.props.record.data.golden_input_mode === "ai";
    }

    get hasClaudeTrajectory() {
        return this.state.hasClaudeTrajectory;
    }

    get canGenerate() {
        return this.isAiMode && !this.isReadonly && this.hasClaudeTrajectory;
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
                this._buildSpawnTree();
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
