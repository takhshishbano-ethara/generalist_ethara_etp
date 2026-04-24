/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { SandboxCard } from "../components/sandbox_card/sandbox_card";
import { GogAuthDialog } from "../components/gog_auth_dialog/gog_auth_dialog";
import { clearChatSession } from "../chat_widget/chat_widget";
import { rpc } from "@web/core/network/rpc";

const MODEL_TABS = [
    { type: "glm", label: "GLM 5", icon: "fa-cube" },
];

const STATUS_POLL_INTERVAL_MS = 5000;

export class TaskDashboard extends Component {
    static template = "atlas.TaskDashboard";
    static components = { SandboxCard, GogAuthDialog };
    static props = { ...standardWidgetProps };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.modelTabs = MODEL_TABS;
        this._pollTimer = null;

        this.state = useState({
            activeTab: "glm",
            loadingSandbox: {},
            sandboxes: {},
            showGogAuth: false,
            gogAuthDone: false,
        });

        this._onSandboxStatusChanged = (ev) => {
            this._handleSandboxStatusChanged(ev.detail);
        };
        this.env.bus.addEventListener(
            "ATLAS:SANDBOX_STATUS_CHANGED",
            this._onSandboxStatusChanged,
        );

        onMounted(() => {
            this._loadSandboxes();
            this._checkGogAuthStatus();
        });
        onWillUnmount(() => {
            this._stopPolling();
            this.env.bus.removeEventListener(
                "ATLAS:SANDBOX_STATUS_CHANGED",
                this._onSandboxStatusChanged,
            );
        });
    }

    _handleSandboxStatusChanged(payload) {
        const sandboxId = payload.sandbox_id;
        delete this.state.loadingSandbox[sandboxId];
        this._loadSandboxes();
        this.props.record.load();
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
            "atlas.sandbox",
            [["atlas_id", "=", this.taskId]],
            [
                "id", "model_type", "docker_status", "docker_port",
                "docker_gateway_token",
                "docker_ws_url", "docker_error", "docker_workdir",
                "session_status", "docker_compose_project",
            ],
        );

        await this.orm.call("atlas.atlas", "ensure_sandboxes", [[this.taskId]]);

        if (sandboxes.length === 0 || sandboxes.length < 1) {
            sandboxes = await this.orm.searchRead(
                "atlas.sandbox",
                [["atlas_id", "=", this.taskId]],
                [
                    "id", "model_type", "docker_status", "docker_port",
                    "docker_gateway_token",
                    "docker_ws_url", "docker_error", "docker_workdir",
                    "session_status", "docker_compose_project",
                ],
            );
        }

        const needsReconcile = sandboxes.filter(
            (sb) => sb.docker_status === "starting" || sb.docker_status === "running" || sb.docker_status === "error"
        );
        if (needsReconcile.length > 0) {
            try {
                const ids = needsReconcile.map((sb) => sb.id);
                const statusMap = await this.orm.call(
                    "atlas.sandbox", "action_check_status", [ids]
                );
                for (const sb of sandboxes) {
                    if (statusMap && statusMap[sb.id] && statusMap[sb.id] !== sb.docker_status) {
                        sb.docker_status = statusMap[sb.id];
                    }
                }
            } catch (e) {
                console.warn("[atlas-dashboard] Status reconciliation failed:", e);
            }
        }

        const map = {};
        let hasStarting = false;
        for (const sb of sandboxes) {
            map[sb.model_type] = sb;
            if (sb.docker_status === "starting") {
                this.state.loadingSandbox[sb.id] = true;
                hasStarting = true;
            }
        }
        this.state.sandboxes = map;

        if (hasStarting) {
            this._startPolling();
        } else {
            this._stopPolling();
        }
    }

    _startPolling() {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => this._pollStatus(), STATUS_POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _pollStatus() {
        const startingSandboxes = Object.values(this.state.sandboxes).filter(
            (sb) => sb.docker_status === "starting"
        );
        if (startingSandboxes.length === 0) {
            this._stopPolling();
            return;
        }

        try {
            const ids = startingSandboxes.map((sb) => sb.id);
            const statusMap = await this.orm.call(
                "atlas.sandbox", "action_check_status", [ids]
            );
            let anyChanged = false;
            for (const sb of Object.values(this.state.sandboxes)) {
                if (statusMap && statusMap[sb.id] && statusMap[sb.id] !== sb.docker_status) {
                    sb.docker_status = statusMap[sb.id];
                    anyChanged = true;
                    if (statusMap[sb.id] !== "starting") {
                        delete this.state.loadingSandbox[sb.id];
                    }
                }
            }
            if (anyChanged) {
                const allIds = Object.values(this.state.sandboxes).map((sb) => sb.id);
                const freshData = await this.orm.searchRead(
                    "atlas.sandbox",
                    [["id", "in", allIds]],
                    [
                        "id", "model_type", "docker_status", "docker_port",
                        "docker_gateway_token",
                        "docker_ws_url", "docker_error", "docker_workdir",
                        "session_status", "docker_compose_project",
                    ],
                );
                for (const fresh of freshData) {
                    if (this.state.sandboxes[fresh.model_type]) {
                        Object.assign(this.state.sandboxes[fresh.model_type], fresh);
                    }
                }
            }
        } catch (e) {
            console.warn("[atlas-dashboard] Poll status failed:", e);
        }

        const stillStarting = Object.values(this.state.sandboxes).some(
            (sb) => sb.docker_status === "starting"
        );
        if (!stillStarting) {
            this._stopPolling();
        }
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
                disabled: false,
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
            disabled: false,
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
        const activeSandbox = Object.values(this.state.sandboxes).find(
            (sb) => sb.id !== sandboxId && (sb.docker_status === "running" || sb.docker_status === "starting")
        );
        if (activeSandbox) {
            const activeLabel = MODEL_TABS.find((t) => t.type === activeSandbox.model_type)?.label || activeSandbox.model_type;
            this.notification.add(
                `Cannot start: ${activeLabel} sandbox is already ${activeSandbox.docker_status}. Stop it first.`,
                { type: "warning" },
            );
            return;
        }
        this.state.loadingSandbox[sandboxId] = true;
        this._setSandboxStatus(sandboxId, "starting");
        this._startPolling();
        clearChatSession(sandboxId);
        try {
            await this.orm.call("atlas.sandbox", "action_start_sandbox", [[sandboxId]]);
            await this._loadSandboxes();
        } catch (e) {
            delete this.state.loadingSandbox[sandboxId];
            this._setSandboxStatus(sandboxId, "error");
            delete this.state.loadingSandbox[sandboxId];
            this.notification.add(
                e.data?.message || e.message || "Failed to start sandbox",
                { type: "danger" }
            );
        }
    }

    async onStopSandbox(sandboxId) {
        if (!sandboxId) return;
        this.state.loadingSandbox[sandboxId] = true;
        try {
            await clearChatSession(sandboxId);
            await this.orm.call("atlas.sandbox", "action_stop_sandbox", [[sandboxId]]);
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

    get hasAnySandboxRunning() {
        return Object.values(this.state.sandboxes).some(
            (sb) => sb.docker_status === "running" || sb.docker_status === "starting"
        );
    }

    async _checkGogAuthStatus() {
        if (!this.taskId) return;
        try {
            const data = await rpc("/atlas/gog/status", { task_id: this.taskId });
            this.state.gogAuthDone = !!data.authenticated;
        } catch (e) {
            console.warn("[atlas-dashboard] Failed to check gog auth status:", e);
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
        this.state.gogAuthDone = true;
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
registry.category("view_widgets").add("atlas_task_dashboard", taskDashboardDef);
