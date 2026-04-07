/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, onWillUpdateProps, useState } from "@odoo/owl";

const POLL_STAGES = new Set(["stage3", "stage5", "stage6"]);
const POLL_INTERVAL = 3000;

export class AutoRefresh extends Component {
    static template = "commit0_pipeline.AutoRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this._interval = null;
        this._polling = false;
        this._reloading = false;
        this.state = useState({ active: false });
        onMounted(() => this._checkAndPoll());
        onWillUpdateProps(() => this._checkAndPoll());
        onWillUnmount(() => this._stop());
    }

    _checkAndPoll() {
        const stage = this.props.record.data[this.props.name];
        if (POLL_STAGES.has(stage)) {
            this._start();
        } else {
            this._stop();
        }
    }

    _start() {
        if (this._polling) return;
        this._polling = true;
        this.state.active = true;
        this._interval = setInterval(() => this._poll(), POLL_INTERVAL);
    }

    async _poll() {
        if (this._reloading) return;
        this._reloading = true;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
        } catch {
            this._stop();
        } finally {
            this._reloading = false;
            this._checkAndPoll();
        }
    }

    _stop() {
        this._polling = false;
        this.state.active = false;
        if (this._interval) {
            clearInterval(this._interval);
            this._interval = null;
        }
    }
}

export const autoRefreshField = {
    component: AutoRefresh,
    displayName: "Auto Refresh",
    supportedTypes: ["selection", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("auto_refresh", autoRefreshField);
