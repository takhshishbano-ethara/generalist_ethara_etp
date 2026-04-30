/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const PIPELINE_POLL_STAGES = new Set([
    "fetch_prs", "filter_prs", "discover_tags",
    "group_prs", "fetch_issues", "build_dataset",
    // Phase 2 — Docker Build & Test
    "phase2_build", "phase2_test", "phase2_report",
    // Phase 3 — Trajectory Generation
    "phase3_infer", "phase3_eval", "phase3_summary",
]);

const EVAL_POLL_STAGES = new Set([
    "building_images", "running_instances", "generating_reports",
]);

const STAGING_POLL_STAGES = new Set([
    "testing", "evaluating",
]);

const FALLBACK_POLL_INTERVAL = 10000;
const BURST_POLL_INTERVAL = 2000;
const BURST_POLL_DURATION = 10000;

export class AuroraAutoRefresh extends Component {
    static template = "aurora.AuroraAutoRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this.busService = useService("bus_service");
        this._fallbackTimer = null;
        this._burstTimeout = null;
        this._reloading = false;
        this._busSubscribed = false;
        this._busChannel = null;
        this._busEventType = null;
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

    _isEvaluation() {
        return this.props.record.resModel === "aurora.evaluation";
    }

    _isStaging() {
        return this.props.record.resModel === "aurora.harness.staging";
    }

    _shouldPoll() {
        if (document.hidden) return false;
        const record = this.props.record;
        if (record.isNew) return false;
        const stage = record.data[this.props.name];
        if (this._isEvaluation()) {
            return EVAL_POLL_STAGES.has(stage);
        }
        if (this._isStaging()) {
            return STAGING_POLL_STAGES.has(stage);
        }
        return PIPELINE_POLL_STAGES.has(stage);
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
        if (this._isEvaluation()) {
            this._busChannel = `aurora_evaluation_${recId}`;
            this._busEventType = "aurora_evaluation_update";
        } else if (this._isStaging()) {
            this._busChannel = `aurora_harness_staging_${recId}`;
            this._busEventType = "aurora_harness_staging_update";
        } else {
            this._busChannel = `aurora_pipeline_${recId}`;
            this._busEventType = "aurora_pipeline_update";
        }
        this.busService.addChannel(this._busChannel);
        this.busService.subscribe(this._busEventType, this._onBusNotification);
        this._busSubscribed = true;

        this._startFallback();
    }

    async _onBusNotification(payload) {
        const recId = this.props.record.resId;
        let idField;
        if (this._isEvaluation()) {
            idField = "evaluation_id";
        } else if (this._isStaging()) {
            idField = "staging_id";
        } else {
            idField = "pipeline_id";
        }
        if (payload[idField] !== recId) return;
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
        const tick = async () => {
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
        };
        this._fallbackTimer = setInterval(tick, BURST_POLL_INTERVAL);
        this._burstTimeout = setTimeout(() => {
            this._burstTimeout = null;
            if (!this._fallbackTimer) return;
            clearInterval(this._fallbackTimer);
            this._fallbackTimer = setInterval(tick, FALLBACK_POLL_INTERVAL);
        }, BURST_POLL_DURATION);
    }

    _stopFallback() {
        if (this._fallbackTimer) {
            clearInterval(this._fallbackTimer);
            this._fallbackTimer = null;
        }
        if (this._burstTimeout) {
            clearTimeout(this._burstTimeout);
            this._burstTimeout = null;
        }
    }

    _cleanup() {
        this.state.active = false;
        this._stopFallback();
        if (this._busSubscribed) {
            if (this._busEventType) {
                this.busService.unsubscribe(this._busEventType, this._onBusNotification);
            }
            if (this._busChannel) {
                this.busService.deleteChannel(this._busChannel);
                this._busChannel = null;
            }
            this._busEventType = null;
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
