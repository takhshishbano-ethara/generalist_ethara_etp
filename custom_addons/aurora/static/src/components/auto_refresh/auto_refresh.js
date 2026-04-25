/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const POLL_STAGES = new Set([
    // Phase 1 — Data Collection
    "fetch_prs", "filter_prs", "discover_tags",
    "group_prs", "fetch_issues", "build_dataset",
    // Phase 2 — Docker Build & Test
    "phase2_build", "phase2_test", "phase2_report",
    // Phase 3 — Trajectory Generation
    "phase3_infer", "phase3_eval", "phase3_summary",
]);

const FALLBACK_POLL_INTERVAL = 10000;

export class AuroraAutoRefresh extends Component {
    static template = "aurora.AuroraAutoRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this.busService = useService("bus_service");
        this._fallbackTimer = null;
        this._reloading = false;
        this._busSubscribed = false;
        this._busChannel = null;
        this.state = useState({ active: false });

        this._onBusNotification = this._onBusNotification.bind(this);

        this._onVisibilityChange = () => {
            if (document.hidden) {
                this._stopFallback();
            } else {
                this._checkAndStart();
            }
        };

        onMounted(() => {
            document.addEventListener("visibilitychange", this._onVisibilityChange);
            this._checkAndStart();
        });
        onWillUnmount(() => {
            document.removeEventListener("visibilitychange", this._onVisibilityChange);
            this._cleanup();
        });
    }

    _shouldPoll() {
        if (document.hidden) return false;
        const record = this.props.record;
        if (record.isNew) return false;
        const stage = record.data[this.props.name];
        return POLL_STAGES.has(stage);
    }

    _checkAndStart() {
        if (this._shouldPoll()) {
            this._subscribe();
        } else {
            this._cleanup();
        }
    }

    _subscribe() {
        if (this._busSubscribed) return;
        this.state.active = true;

        const recId = this.props.record.resId;
        this._busChannel = `aurora_pipeline_${recId}`;
        this.busService.addChannel(this._busChannel);
        this.busService.subscribe("aurora_pipeline_update", this._onBusNotification);
        this._busSubscribed = true;

        this._startFallback();
    }

    async _onBusNotification(payload) {
        const recId = this.props.record.resId;
        if (payload.pipeline_id !== recId) return;
        if (this._reloading) return;
        this._reloading = true;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
        } catch {
            // ignore reload errors
        } finally {
            this._reloading = false;
        }
        if (!this._shouldPoll()) {
            this._cleanup();
        }
    }

    _startFallback() {
        if (this._fallbackTimer) return;
        this._fallbackTimer = setInterval(async () => {
            if (this._reloading || document.hidden) return;
            this._reloading = true;
            try {
                await this.props.record.load();
                this.props.record.model.notify();
            } catch {
                // ignore reload errors
            } finally {
                this._reloading = false;
            }
            if (!this._shouldPoll()) {
                this._cleanup();
            }
        }, FALLBACK_POLL_INTERVAL);
    }

    _stopFallback() {
        if (this._fallbackTimer) {
            clearInterval(this._fallbackTimer);
            this._fallbackTimer = null;
        }
    }

    _cleanup() {
        this.state.active = false;
        this._stopFallback();
        if (this._busSubscribed) {
            this.busService.unsubscribe("aurora_pipeline_update", this._onBusNotification);
            if (this._busChannel) {
                this.busService.deleteChannel(this._busChannel);
                this._busChannel = null;
            }
            this._busSubscribed = false;
        }
    }
}

export const auroraAutoRefreshField = {
    component: AuroraAutoRefresh,
    displayName: "Auto Refresh",
    supportedTypes: ["selection", "char"],
    extractProps: () => ({}),
};

registry.category("fields").add("aurora_auto_refresh", auroraAutoRefreshField);
