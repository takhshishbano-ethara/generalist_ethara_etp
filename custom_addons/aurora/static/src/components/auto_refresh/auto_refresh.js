/** @odoo-module */

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";

const PIPELINE_POLL_STAGES = new Set([
    "fetch_prs", "filter_prs", "discover_tags",
    "group_prs", "fetch_issues", "build_dataset",
    "phase2_build", "phase2_test", "phase2_report",
    "phase3_infer", "phase3_eval", "phase3_summary",
]);

const EVAL_POLL_STAGES = new Set([
    "building_images", "running_instances", "generating_reports",
]);

const STAGING_POLL_STAGES = new Set([
    "testing", "evaluating",
]);

const POLL_INTERVAL = 15000;
const MAX_CONSECUTIVE_FAILURES = 5;

export class AuroraAutoRefresh extends Component {
    static template = "aurora.AuroraAutoRefresh";
    static props = { ...standardFieldProps };

    setup() {
        this._timer = null;
        this._reloading = false;
        this._failCount = 0;
        this.state = useState({ active: false });

        this._onVisibilityChange = () => {
            if (document.hidden) {
                this._stop();
            } else {
                this._checkAndStart();
            }
        };

        this._onBusUpdate = (ev) => {
            const payload = ev.detail;
            if (this._matchesRecord(payload)) {
                this._reload();
            }
        };

        onMounted(() => {
            document.addEventListener("visibilitychange", this._onVisibilityChange);
            this.env.bus.addEventListener("AURORA:STATUS_UPDATE", this._onBusUpdate);
            this._checkAndStart();
        });
        onWillUnmount(() => {
            document.removeEventListener("visibilitychange", this._onVisibilityChange);
            this.env.bus.removeEventListener("AURORA:STATUS_UPDATE", this._onBusUpdate);
            this._stop();
        });
    }

    _matchesRecord(payload) {
        if (!payload) return false;
        const record = this.props.record;
        return payload.model === record.resModel && payload.id === record.resId;
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
            this._start();
        } else {
            this._stop();
        }
    }

    async _reload() {
        if (this._reloading || document.hidden) return;
        this._reloading = true;
        try {
            await this.props.record.load();
            this.props.record.model.notify();
            this._failCount = 0;
        } catch {
            this._failCount += 1;
            if (this._failCount >= MAX_CONSECUTIVE_FAILURES) {
                this._stop();
                return;
            }
        } finally {
            this._reloading = false;
        }
        if (!this._shouldPoll()) {
            this._stop();
        }
    }

    _start() {
        if (this._timer) return;
        this.state.active = true;
        this._failCount = 0;
        this._timer = setInterval(() => this._reload(), POLL_INTERVAL);
    }

    _stop() {
        this.state.active = false;
        if (this._timer) {
            clearInterval(this._timer);
            this._timer = null;
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
