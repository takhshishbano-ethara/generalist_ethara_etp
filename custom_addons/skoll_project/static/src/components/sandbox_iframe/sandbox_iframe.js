/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

import { Component, useState, useRef, onWillUpdateProps, onWillDestroy } from "@odoo/owl";

const LOADING_STEPS = [
    { label: "Initializing sandbox environment", pct: 10 },
    { label: "Preparing workspace", pct: 25 },
    { label: "Starting Docker containers", pct: 40 },
    { label: "Building services", pct: 55 },
    { label: "Running health checks", pct: 70 },
    { label: "Connecting to gateway", pct: 85 },
    { label: "Loading browser session", pct: 95 },
];

const STEP_INTERVAL_MS = 4000;

export class SandboxIframe extends Component {
    static template = "skoll.SandboxIframe";
    static props = {
        ...standardFieldProps,
    };

    setup() {
        this.rootRef = useRef("root");
        this.state = useState({
            loading: false,
            error: false,
            currentStep: 0,
            progressPct: 0,
            showProgress: false,
        });
        this._stepTimer = null;

        this._syncProgress(this.props);

        onWillUpdateProps((nextProps) => {
            this._syncProgress(nextProps);
        });

        onWillDestroy(() => {
            this._clearStepTimer();
        });
    }

    _syncProgress(props) {
        const status = props.record.data.docker_status || "stopped";
        const url = props.record.data[props.name] || "";

        if (status === "starting") {
            if (!this.state.showProgress) {
                this.state.showProgress = true;
                this.state.currentStep = 0;
                this.state.progressPct = LOADING_STEPS[0].pct;
                this.state.loading = false;
                this.state.error = false;
                this._startStepTimer();
            }
        } else if (status === "running" && url) {
            if (this.state.showProgress && this.state.currentStep < LOADING_STEPS.length - 1) {
                this._clearStepTimer();
                this.state.currentStep = LOADING_STEPS.length - 1;
                this.state.progressPct = LOADING_STEPS[LOADING_STEPS.length - 1].pct;
            }
            this.state.loading = true;
        } else if (status === "stopped" || status === "error") {
            this._clearStepTimer();
            this.state.showProgress = false;
            this.state.currentStep = 0;
            this.state.progressPct = 0;
            this.state.loading = false;
            if (status === "error") {
                this.state.error = true;
            }
        }
    }

    _startStepTimer() {
        this._clearStepTimer();
        this._stepTimer = setInterval(() => {
            const nextStep = this.state.currentStep + 1;
            if (nextStep < LOADING_STEPS.length) {
                this.state.currentStep = nextStep;
                this.state.progressPct = LOADING_STEPS[nextStep].pct;
            } else {
                this._clearStepTimer();
            }
        }, STEP_INTERVAL_MS);
    }

    _clearStepTimer() {
        if (this._stepTimer) {
            clearInterval(this._stepTimer);
            this._stepTimer = null;
        }
    }

    get dashboardUrl() {
        return this.props.record.data[this.props.name] || "";
    }

    get dockerStatus() {
        return this.props.record.data.docker_status || "stopped";
    }

    get isRunning() {
        return this.dockerStatus === "running";
    }

    get isStarting() {
        return this.dockerStatus === "starting";
    }

    get shouldShowIframe() {
        return this.isRunning && Boolean(this.dashboardUrl);
    }

    get truncatedUrl() {
        const url = this.dashboardUrl;
        if (url.length <= 80) {
            return url;
        }
        return `${url.slice(0, 77)}...`;
    }

    get totalSteps() {
        return LOADING_STEPS.length;
    }

    get completedSteps() {
        return this.state.currentStep + 1;
    }

    get currentStepLabel() {
        return LOADING_STEPS[this.state.currentStep].label;
    }

    get progressPercent() {
        return this.state.progressPct;
    }

    get progressBarStyle() {
        return `width: ${this.state.progressPct}%`;
    }

    onIframeLoad() {
        this.state.loading = false;
        this.state.error = false;
        this._clearStepTimer();
        this.state.showProgress = false;
        this.state.progressPct = 100;
    }

    onIframeError() {
        this.state.loading = false;
        this.state.error = true;
        this._clearStepTimer();
        this.state.showProgress = false;
    }

    toggleFullscreen() {
        const target = this.rootRef.el;
        if (!target || !document.fullscreenEnabled) {
            return;
        }

        if (document.fullscreenElement) {
            document.exitFullscreen();
            return;
        }

        target.requestFullscreen();
    }
}

export const sandboxIframeField = {
    component: SandboxIframe,
    displayName: _t("Sandbox Iframe"),
    supportedTypes: ["char"],
    extractProps: () => ({}),
};

registry.category("fields").add("skoll_sandbox_iframe", sandboxIframeField);
