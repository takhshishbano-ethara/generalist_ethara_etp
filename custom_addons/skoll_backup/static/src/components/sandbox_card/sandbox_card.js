/** @odoo-module */
import { Component } from "@odoo/owl";
import { SkollChatWidget } from "../../chat_widget/chat_widget";

export class SandboxCard extends Component {
    static template = "skoll.SandboxCard";
    static components = { SkollChatWidget };
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
            window.open(`/skoll/chat/export_session?sandbox_id=${this.props.sandboxId}`, "_blank");
        }
    }
}
