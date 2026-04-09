/** @odoo-module */
import { Component, useState } from "@odoo/owl";
import { TalosChatWidget } from "../../chat_widget/chat_widget";

const TABS = [
    { id: "chat", label: "Chat", icon: "fa-comments" },
    { id: "dashboard", label: "Dashboard", icon: "fa-tachometer" },
    { id: "turns", label: "Turns", icon: "fa-list-ol" },
];

export class SandboxCard extends Component {
    static template = "talos.SandboxCard";
    static components = { TalosChatWidget };
    static props = {
        sandboxId: Number,
        modelType: String,
        modelLabel: String,
        dockerStatus: String,
        sessionStatus: String,
        dockerWsUrl: { type: [String, Boolean], optional: true },
        gatewayToken: { type: [String, Boolean], optional: true },
        dashboardUrl: { type: [String, Boolean], optional: true },
        dockerError: { type: [String, Boolean], optional: true },
        disabled: Boolean,
        turnData: Array,
        loading: Boolean,
        onStart: Function,
        onStop: Function,
    };

    setup() {
        this.state = useState({ activeTab: "chat" });
        this.tabs = TABS;
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

    onTabClick(tabId) {
        this.state.activeTab = tabId;
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
}
