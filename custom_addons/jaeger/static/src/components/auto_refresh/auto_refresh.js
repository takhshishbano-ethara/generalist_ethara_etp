/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onPatched, onWillUnmount, useState } from "@odoo/owl";

const POLL_STATUSES = new Set([
    "running", "building", "queued", "generating",
    "dispatched", "evaluating", "converting",
]);
const POLL_INTERVAL = 3000;
const MAX_CONSECUTIVE_FAILURES = 5;

export class JaegerAutoRefresh extends Component {
    static template = "jaeger.AutoRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this._interval = null;
        this._polling = false;
        this._reloading = false;
        this._failCount = 0;
        this.state = useState({ active: false });

        onMounted(() => this._checkAndPoll());
        onPatched(() => this._checkAndPoll());
        onWillUnmount(() => this._stop());
    }

    _shouldPoll() {
        const data = this.props.record.data;
        return POLL_STATUSES.has(data[this.props.name])
            || POLL_STATUSES.has(data.crawl_status)
            || POLL_STATUSES.has(data.docker_build_status)
            || POLL_STATUSES.has(data.base_image_status)
            || POLL_STATUSES.has(data.test_execution_status)
            || POLL_STATUSES.has(data.dataset_status);
    }

    _checkAndPoll() {
        if (this._shouldPoll()) {
            this._start();
        } else {
            this._stop();
        }
    }

    _start() {
        if (this._polling) return;
        this._polling = true;
        this._failCount = 0;
        this.state.active = true;
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
                this._stop();
                return;
            }
        } finally {
            this._reloading = false;
            if (this._polling) {
                this._checkAndPoll();
            }
        }
    }

    _stop() {
        this._polling = false;
        this._failCount = 0;
        this.state.active = false;
        if (this._interval) {
            clearInterval(this._interval);
            this._interval = null;
        }
    }
}

export const jaegerAutoRefreshField = {
    component: JaegerAutoRefresh,
    displayName: "Auto Refresh",
    supportedTypes: ["selection", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("jaeger_auto_refresh", jaegerAutoRefreshField);
