/** @odoo-module */
import { Component } from "@odoo/owl";
import { KenseiChatWidget } from "../../chat_widget/chat_widget";

export class SandboxCard extends Component {
    static template = "kensei.SandboxCard";
    static components = { KenseiChatWidget };
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
            window.open(`/kensei/chat/export_session?sandbox_id=${this.props.sandboxId}`, "_blank");
        }
    }
}
