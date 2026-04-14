/** @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { SandboxCard } from "../components/sandbox_card/sandbox_card";
import { clearChatSession } from "../chat_widget/chat_widget";

const MODEL_TABS = [
    { type: "claude", label: "Claude 4.6", icon: "fa-microchip" },
    { type: "glm", label: "GLM 5", icon: "fa-cube" },
    { type: "1p", label: "1P", icon: "fa-flask" },
];

export class TaskDashboard extends Component {
    static template = "talos.TaskDashboard";
    static components = { SandboxCard };
    static props = { ...standardWidgetProps };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.modelTabs = MODEL_TABS;

        this.state = useState({
            activeTab: "claude",
            loadingSandbox: {},
            sandboxes: {},
        });

        onMounted(() => this._loadSandboxes());
    }

    get taskId() {
        return this.props.record.resId;
    }

    get taskEmail() {
        return this.props.record.data.email || "";
    }

    async _loadSandboxes() {
        if (!this.taskId) return;

        let sandboxes = await this.orm.searchRead(
            "talos.sandbox",
            [["talos_id", "=", this.taskId]],
            [
                "id", "model_type", "docker_status", "docker_port",
                "docker_gateway_token",
                "docker_ws_url", "docker_error", "docker_workdir",
                "session_status", "docker_compose_project",
            ],
        );

        if (sandboxes.length === 0) {
            console.log("[talos-dashboard] No sandboxes found, creating...");
            await this.orm.call("talos.talos", "_ensure_sandboxes", [[this.taskId]]);
            sandboxes = await this.orm.searchRead(
                "talos.sandbox",
                [["talos_id", "=", this.taskId]],
                [
                    "id", "model_type", "docker_status", "docker_port",
                    "docker_gateway_token",
                    "docker_ws_url", "docker_error", "docker_workdir",
                    "session_status", "docker_compose_project",
                ],
            );
        }

        const map = {};
        for (const sb of sandboxes) {
            map[sb.model_type] = sb;
        }
        this.state.sandboxes = map;
    }

    _buildSandboxProps(modelType) {
        const sb = this.state.sandboxes[modelType];
        if (!sb) {
            return {
                sandboxId: 0,
                taskId: this.taskId,
                taskEmail: this.taskEmail,
                modelType,
                modelLabel: MODEL_TABS.find((t) => t.type === modelType)?.label || modelType,
                dockerStatus: "stopped",
                sessionStatus: "not_started",
                dockerWsUrl: false,
                gatewayToken: false,
                dockerError: false,
                disabled: modelType === "1p",
                loading: false,
            };
        }

        return {
            sandboxId: sb.id,
            taskId: this.taskId,
            taskEmail: this.taskEmail,
            modelType: sb.model_type,
            modelLabel: MODEL_TABS.find((t) => t.type === modelType)?.label || modelType,
            dockerStatus: sb.docker_status || "stopped",
            sessionStatus: sb.session_status || "not_started",
            dockerWsUrl: sb.docker_ws_url || false,
            gatewayToken: sb.docker_gateway_token || false,
            dockerError: sb.docker_error || false,
            disabled: modelType === "1p",
            loading: !!this.state.loadingSandbox[sb.id],
        };
    }

    onTabClick(modelType) {
        this.state.activeTab = modelType;
    }

    async onStartSandbox(sandboxId) {
        if (!sandboxId) {
            this.notification.add("Sandbox not found. Save the task first.", { type: "warning" });
            return;
        }
        this.state.loadingSandbox[sandboxId] = true;
        this._setSandboxStatus(sandboxId, "starting");
        clearChatSession(sandboxId);
        try {
            await this.orm.call("talos.sandbox", "action_start_sandbox", [[sandboxId]]);
            await this._loadSandboxes();
            await this.props.record.load();
        } catch (e) {
            this._setSandboxStatus(sandboxId, "error");
            this.notification.add(
                e.data?.message || e.message || "Failed to start sandbox",
                { type: "danger" }
            );
        } finally {
            delete this.state.loadingSandbox[sandboxId];
        }
    }

    async onStopSandbox(sandboxId) {
        if (!sandboxId) return;
        this.state.loadingSandbox[sandboxId] = true;
        try {
            await clearChatSession(sandboxId);
            await this.orm.call("talos.sandbox", "action_stop_sandbox", [[sandboxId]]);
            window.open(`/talos/chat/export_session?sandbox_id=${sandboxId}`, "_blank");
            await this._loadSandboxes();
            await this.props.record.load();
        } catch (e) {
            this.notification.add(
                e.data?.message || e.message || "Failed to stop sandbox",
                { type: "danger" }
            );
        } finally {
            delete this.state.loadingSandbox[sandboxId];
        }
    }

    _setSandboxStatus(sandboxId, status) {
        for (const sb of Object.values(this.state.sandboxes)) {
            if (sb.id === sandboxId) {
                sb.docker_status = status;
                break;
            }
        }
    }
}

export const taskDashboardDef = { component: TaskDashboard };
registry.category("view_widgets").add("talos_task_dashboard", taskDashboardDef);
