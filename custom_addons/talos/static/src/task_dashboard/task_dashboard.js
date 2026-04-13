/** @odoo-module */
import { Component, useState, onMounted } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { rpc } from "@web/core/network/rpc";
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
            turnCache: {},
        });

        onMounted(() => this._loadSandboxes());
    }

    get taskId() {
        return this.props.record.resId;
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

        console.log("[talos-dashboard] Loaded sandboxes:", sandboxes);

        const map = {};
        for (const sb of sandboxes) {
            map[sb.model_type] = sb;
        }
        this.state.sandboxes = map;

        for (const sb of sandboxes) {
            try {
                const result = await rpc("/talos/chat/history", { sandbox_id: sb.id });
                this.state.turnCache[sb.id] = (result.turns || []).map((t) => ({
                    id: t.id,
                    turn_number: t.turn_number,
                    prompt: t.prompt,
                    response: t.response,
                    status: t.status,
                }));
            } catch {
                this.state.turnCache[sb.id] = [];
            }
        }
    }

    _buildSandboxProps(modelType) {
        const sb = this.state.sandboxes[modelType];
        if (!sb) {
            return {
                sandboxId: 0,
                modelType,
                modelLabel: MODEL_TABS.find((t) => t.type === modelType)?.label || modelType,
                dockerStatus: "stopped",
                sessionStatus: "not_started",
                dockerWsUrl: false,
                gatewayToken: false,
                dockerError: false,
                disabled: modelType === "1p",
                turnData: [],
                loading: false,
            };
        }

        return {
            sandboxId: sb.id,
            modelType: sb.model_type,
            modelLabel: MODEL_TABS.find((t) => t.type === modelType)?.label || modelType,
            dockerStatus: sb.docker_status || "stopped",
            sessionStatus: sb.session_status || "not_started",
            dockerWsUrl: sb.docker_ws_url || false,
            gatewayToken: sb.docker_gateway_token || false,
            dockerError: sb.docker_error || false,
            disabled: modelType === "1p",
            turnData: this.state.turnCache[sb.id] || [],
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
        try {
            await this.orm.call("talos.sandbox", "action_start_sandbox", [[sandboxId]]);
            await this._loadSandboxes();
            await this.props.record.load();
        } catch (e) {
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
            const hasTurns = (this.state.turnCache[sandboxId] || []).length > 0;
            if (hasTurns) {
                window.open(`/talos/chat/export_session?sandbox_id=${sandboxId}`, "_blank");
            }

            await this.orm.call("talos.sandbox", "action_stop_sandbox", [[sandboxId]]);

            clearChatSession(sandboxId);
            this.state.turnCache[sandboxId] = [];

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
}

export const taskDashboardDef = { component: TaskDashboard };
registry.category("view_widgets").add("talos_task_dashboard", taskDashboardDef);
