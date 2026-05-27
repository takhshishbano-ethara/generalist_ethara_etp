/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";

const NON_TERMINAL = new Set(["queued", "submitting", "processing", "downloading"]);
const POLL_FALLBACK_MS = 10000;
const COALESCE_MS = 500;

const STEP_LABELS = {
    queued: "Queued",
    submitting: "Submitting to OpenRouter",
    processing: "Processing on OpenRouter",
    downloading: "Downloading and uploading to S3",
};

export class T2AVLiveStatus extends Component {
    static template = "t2av.LiveStatus";
    static props = { ...standardFieldProps };

    setup() {
        this.bus = useService("bus_service");
        this.state = useState({ label: "", subText: "", busOk: false });
        this._channel = "t2av.generation";
        this._handler = (ev) => this._onBusMessage(ev);
        this._pollTimer = null;
        this._lastReload = 0;

        onMounted(() => this._subscribe());
        onWillUnmount(() => this._unsubscribe());

        this._updateLabel();
    }

    _subscribe() {
        try {
            this.bus.addChannel(this._channel);
            this.bus.addEventListener("notification", this._handler);
            this.state.busOk = true;
        } catch (e) {
            this.state.busOk = false;
            this._startFallbackPoll();
        }
    }

    _unsubscribe() {
        try {
            this.bus.removeEventListener("notification", this._handler);
            this.bus.deleteChannel(this._channel);
        } catch (e) {
            // ignore
        }
        this._stopFallbackPoll();
    }

    async _onBusMessage(ev) {
        const notifications = ev.detail || [];
        const recId = this.props.record.resId;
        for (const n of notifications) {
            if (n.type !== "t2av.generation.update") continue;
            if (!n.payload || n.payload.id !== recId) continue;
            await this._reload();
            return;
        }
    }

    async _reload() {
        const now = Date.now();
        if (now - this._lastReload < COALESCE_MS) return;
        this._lastReload = now;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
        } catch (e) {
            // record may have been deleted or user logged out
        }
        this._updateLabel();
    }

    _startFallbackPoll() {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => this._reload(), POLL_FALLBACK_MS);
    }

    _stopFallbackPoll() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    _updateLabel() {
        const d = this.props.record.data;
        const st = d.state;
        const base = STEP_LABELS[st] || "";
        if (st === "processing" && d.poll_attempts) {
            this.state.label = base;
            this.state.subText = `poll ${d.poll_attempts}`;
        } else {
            this.state.label = base;
            this.state.subText = "";
        }
        if (!NON_TERMINAL.has(st)) this._stopFallbackPoll();
    }
}

const t2avLiveStatus = {
    component: T2AVLiveStatus,
    displayName: "T2AV Live Status",
    supportedTypes: ["selection", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("t2av_live_status", t2avLiveStatus);
