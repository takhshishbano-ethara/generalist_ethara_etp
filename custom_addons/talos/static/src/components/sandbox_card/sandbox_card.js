/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { TalosChatWidget } from "../../chat_widget/chat_widget";
import { GogAuthDialog } from "../gog_auth_dialog/gog_auth_dialog";

export class SandboxCard extends Component {
    static template = "talos.SandboxCard";
    static components = { TalosChatWidget, GogAuthDialog };
    static props = {
        sandboxId: Number,
        taskId: Number,
        taskEmail: { type: [String, Boolean], optional: true },
        modelType: String,
        modelLabel: String,
        dockerStatus: String,
        sessionStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
        dockerError: { type: [String, Boolean], optional: true },
        disabled: Boolean,
        loading: Boolean,
        onStart: Function,
        onStop: Function,
    };

    setup() {
        this.state = useState({ showGogAuth: false });
    }

    get isRunning() {
        return this.props.dockerStatus === "running";
    }

    get isStarting() {
        return this.props.dockerStatus === "starting";
    }

    get isStopped() {
        return this.props.dockerStatus === "stopped";
    }

    get statusColor() {
        const map = {
            running: "success",
            starting: "info",
            stopped: "secondary",
            error: "danger",
        };
        return map[this.props.dockerStatus] || "secondary";
    }

    onStartClick() {
        this.props.onStart(this.props.sandboxId);
    }

    onStopClick() {
        this.props.onStop(this.props.sandboxId);
    }

    onExportClick() {
        if (this.props.sandboxId) {
            window.open(`/talos/chat/export_session?sandbox_id=${this.props.sandboxId}`, "_blank");
        }
    }

    onGogAuthClick() {
        this.state.showGogAuth = true;
    }

    onGogAuthClose() {
        this.state.showGogAuth = false;
    }

    onGogAuthSuccess() {
        this.state.showGogAuth = false;
    }
}
