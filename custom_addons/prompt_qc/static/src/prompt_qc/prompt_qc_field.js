/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useRecordObserver } from "@web/model/relational_model/utils";
import { useBus, useService } from "@web/core/utils/hooks";
import { Component, useState } from "@odoo/owl";

import { promptQcStreamBus } from "./prompt_qc_stream_service";

/**
 * prompt_qc result field.
 *
 * Bound to the `response` text field. Owns the "Start QC" button. Clicking it does NOT run the LLM
 * call in the request — it calls `action_enqueue`, which queues the run for the background worker
 * and returns instantly. The judge's tokens then arrive live over the bus (relayed through
 * promptQcStreamBus); on the terminal event we reload the record to show the persisted verdict.
 * No inline SSE, no held HTTP worker — this is what lets many users click at once.
 */
export class PromptQcResultField extends Component {
    static template = "prompt_qc.QcResultField";
    static props = { ...standardFieldProps };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.state = useState({
            running: false,
            streamText: "",
            persisted: "",
        });

        useRecordObserver((record) => {
            this.state.persisted = record.data[this.props.name] || "";
            // Reflect server-side progress (e.g. the form reopened while the run is in flight).
            const st = record.data.state;
            if (st === "queued" || st === "running") {
                this.state.running = true;
            } else if (st === "done" || st === "failed" || st === "draft") {
                this.state.running = false;
            }
        });
        this.state.persisted = this.props.record.data[this.props.name] || "";

        // Live judge output, relayed from the bus. The callback is auto-removed on unmount.
        useBus(promptQcStreamBus, "message", (ev) => this._onStream(ev.detail));
    }

    _onStream(payload) {
        if (!payload || payload.run_id !== this.props.record.resId) {
            return; // a notification for a different run / view
        }
        if (payload.kind === "delta") {
            this.state.running = true;
            this.state.streamText += payload.text || "";
        } else if (payload.kind === "done") {
            this.state.running = false;
            this.state.streamText = "";
            if (payload.state === "failed" && payload.error) {
                this.notification.add(_t("QC failed: ") + payload.error, { type: "danger" });
            }
            // Pull the server-persisted result + final state.
            this.props.record.load();
        }
    }

    get runState() {
        return this.props.record.data.state;
    }

    get hasInputs() {
        const data = this.props.record.data;
        return !!(data.user_prompt && data.user_prompt.trim());
    }

    get canStart() {
        return (
            !this.state.running &&
            this.hasInputs &&
            !["queued", "running"].includes(this.runState)
        );
    }

    get startLabel() {
        return this.state.persisted || this.runState === "failed"
            ? _t("Re-run QC")
            : _t("Start QC");
    }

    async onStart() {
        if (!this.canStart) {
            return;
        }
        // Persist the inputs first so the worker can read them by record id.
        if (this.props.record.isDirty) {
            await this.props.record.save();
        }
        const recordId = this.props.record.resId;
        if (!recordId) {
            this.notification.add(_t("Save the run before starting QC."), { type: "warning" });
            return;
        }

        this.state.running = true;
        this.state.streamText = "";
        try {
            await this.orm.call("prompt.qc.run", "action_enqueue", [[recordId]]);
            this.notification.add(_t("QC queued — streaming the verdict…"), { type: "success" });
        } catch (e) {
            this.state.running = false;
            this.notification.add(
                (e && e.data && e.data.message) || (e && e.message) || _t("Could not start QC."),
                { type: "danger" }
            );
        }
    }
}

export const promptQcResultField = {
    component: PromptQcResultField,
    displayName: _t("Prompt QC Result"),
    supportedTypes: ["text"],
    extractProps: () => ({}),
};

registry.category("fields").add("prompt_qc_result", promptQcResultField);
