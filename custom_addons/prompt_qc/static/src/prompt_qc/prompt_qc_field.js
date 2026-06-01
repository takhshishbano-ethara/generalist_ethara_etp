/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useService } from "@web/core/utils/hooks";
import { Component, useState, onWillUnmount } from "@odoo/owl";

/**
 * prompt_qc result field.
 *
 * Bound to the `response` text field. Owns the "Start QC" button and the SSE stream
 * (fetch + ReadableStream, kensei2/skoll convention). While streaming it shows the live
 * text in a <pre> with a fa-spinner — NO blinking cursor. The final result is persisted
 * server-side on stream completion; this widget reloads the record to show it.
 */
export class PromptQcResultField extends Component {
    static template = "prompt_qc.QcResultField";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.state = useState({
            running: false,
            streamText: "",
            persisted: "",
        });
        this._abort = null;

        useRecordObserver((record) => {
            this.state.persisted = record.data[this.props.name] || "";
        });
        this.state.persisted = this.props.record.data[this.props.name] || "";

        onWillUnmount(() => {
            if (this._abort) this._abort.abort();
        });
    }

    get runState() {
        return this.props.record.data.state;
    }

    get hasInputs() {
        const data = this.props.record.data;
        return !!(data.user_prompt && data.user_prompt.trim());
    }

    get canStart() {
        return !this.state.running && this.hasInputs && this.runState !== "streaming";
    }

    get startLabel() {
        return this.state.persisted || this.runState === "failed"
            ? _t("Re-run QC")
            : _t("Start QC");
    }

    async onStart() {
        if (!this.canStart) return;

        // Persist the inputs first so the server can read them by record id.
        if (this.props.record.isDirty) {
            await this.props.record.save();
        }
        const recordId = this.props.record.resId;
        if (!recordId) {
            this.notification.add(_t("Save the run before starting QC."), { type: "warning" });
            return;
        }

        this._abort = new AbortController();
        this.state.running = true;
        this.state.streamText = "";

        let accumulated = "";
        try {
            const resp = await fetch("/prompt_qc/run/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ record_id: recordId }),
                credentials: "same-origin",
                signal: this._abort.signal,
            });

            if (!resp.ok || !resp.body) {
                this.notification.add(_t("QC request failed: HTTP ") + resp.status, {
                    type: "danger",
                });
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
                            this.state.streamText = accumulated;
                        } else if (payload.type === "error") {
                            this.notification.add(_t("QC error: ") + payload.message, {
                                type: "danger",
                            });
                        }
                    } catch (_e) {
                        // ignore partial / non-JSON lines
                    }
                }
            }
        } catch (err) {
            if (err.name !== "AbortError") {
                this.notification.add(_t("QC stream failed: ") + (err.message || err), {
                    type: "danger",
                });
            }
        } finally {
            this.state.running = false;
            this.state.streamText = "";
            this._abort = null;
            // Reload to pull the server-persisted result + final state.
            await this.props.record.load();
        }
    }

    onStop() {
        if (this._abort) this._abort.abort();
    }
}

export const promptQcResultField = {
    component: PromptQcResultField,
    displayName: _t("Prompt QC Result"),
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("prompt_qc_result", promptQcResultField);
