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
    { type: "claude", label: "Claude Opus 4.7", icon: "fa-microchip" },
    { type: "glm", label: "Kimi K2.6", icon: "fa-cube" },
];

const TRAJECTORY_FIELD_MAP = {
    claude: "claude_trajectory",
    glm: "glm_trajectory",
};

const STATUS_POLL_INTERVAL_MS = 5000;

export class TaskDashboard extends Component {
    static template = "kensei.TaskDashboard";
    static components = { SandboxCard, GogAuthDialog };
    static props = { ...standardWidgetProps };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.modelTabs = MODEL_TABS;
        this._pollTimer = null;

        this.state = useState({
            activeTab: "claude",
            loadingSandbox: {},
            sandboxes: {},
            showGogAuth: false,
            gogAuthDone: false,
            rubrics: [],
            newRubricLabel: "",
            rubricError: "",
        });

        this._onSandboxStatusChanged = (ev) => {
            this._handleSandboxStatusChanged(ev.detail);
        };
        this.env.bus.addEventListener(
            "KENSEI:SANDBOX_STATUS_CHANGED",
            this._onSandboxStatusChanged,
        );

        onMounted(() => {
            this._loadSandboxes();
            this._checkGogAuthStatus();
            this._loadRubrics();
        });
        onWillUnmount(() => {
            this._stopPolling();
            this.env.bus.removeEventListener(
                "KENSEI:SANDBOX_STATUS_CHANGED",
                this._onSandboxStatusChanged,
            );
        });
    }

    async _handleSandboxStatusChanged(payload) {
        const sandboxId = payload.sandbox_id;
        delete this.state.loadingSandbox[sandboxId];
        await this._loadSandboxes();
        await this.props.record.load();

        const status = payload.docker_status || payload.status;
        if (status === "stopped" || status === "exited") {
            const modelType = payload.model_type;
            if (modelType) {
                await this._autoTriggerTaskDescription(modelType);
            }
        }
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
            "kensei.sandbox",
            [["kensei_id", "=", this.taskId]],
            [
                "id", "model_type", "docker_status", "docker_port",
                "docker_gateway_token",
                "docker_ws_url", "docker_error", "docker_workdir",
                "session_status", "docker_compose_project",
            ],
        );

        await this.orm.call("kensei.kensei", "ensure_sandboxes", [[this.taskId]]);

        if (sandboxes.length === 0 || sandboxes.length < MODEL_TABS.length) {
            sandboxes = await this.orm.searchRead(
                "kensei.sandbox",
                [["kensei_id", "=", this.taskId]],
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
                    "kensei.sandbox", "action_check_status", [ids]
                );
                for (const sb of sandboxes) {
                    if (statusMap && statusMap[sb.id] && statusMap[sb.id] !== sb.docker_status) {
                        sb.docker_status = statusMap[sb.id];
                    }
                }
            } catch (e) {
                console.warn("[kensei-dashboard] Status reconciliation failed:", e);
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
                "kensei.sandbox", "action_check_status", [ids]
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
                    "kensei.sandbox",
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
            console.warn("[kensei-dashboard] Poll status failed:", e);
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
            await this.orm.call("kensei.sandbox", "action_start_sandbox", [[sandboxId]]);
            await this._loadSandboxes();
        } catch (e) {
            const msg = e.data?.message || e.message || "";
            const isAlreadyActive = msg.includes("already") && (msg.includes("starting") || msg.includes("running") || msg.includes("progress"));
            if (isAlreadyActive) {
                await this._loadSandboxes();
                this.notification.add(msg, { type: "warning" });
            } else {
                delete this.state.loadingSandbox[sandboxId];
                this._setSandboxStatus(sandboxId, "error");
                this.notification.add(msg || "Failed to start sandbox", { type: "danger" });
            }
        }
    }

    async onStopSandbox(sandboxId) {
        if (!sandboxId) return;
        this.state.loadingSandbox[sandboxId] = true;

        const sandbox = Object.values(this.state.sandboxes).find((sb) => sb.id === sandboxId);
        const modelType = sandbox?.model_type;

        try {
            await clearChatSession(sandboxId);
            await this.orm.call("kensei.sandbox", "action_stop_sandbox", [[sandboxId]]);
            await this._loadSandboxes();
            await this.props.record.load();
            await this._autoTriggerTaskDescription(modelType);
        } catch (e) {
            this.notification.add(
                e.data?.message || e.message || "Failed to stop sandbox",
                { type: "danger" }
            );
        } finally {
            delete this.state.loadingSandbox[sandboxId];
        }
    }

    async _autoTriggerTaskDescription(modelType) {
        if (!modelType) return;

        const fieldName = TRAJECTORY_FIELD_MAP[modelType];
        if (!fieldName) return;

        const recordId = this.taskId;
        if (!recordId) return;

        const raw = this.props.record.data[fieldName];
        if (!raw || !raw.trim()) return;

        let entries;
        try {
            const parsed = JSON.parse(raw);
            entries = Array.isArray(parsed) ? parsed : [parsed];
        } catch (_e) {
            return;
        }

        if (entries.length === 0) return;

        const entryIndex = entries.length - 1;
        const entry = entries[entryIndex];

        if (entry.task_description_status === "pending" || entry.task_description_status === "done") {
            return;
        }

        try {
            await rpc("/kensei/generate_task_description", {
                record_id: recordId,
                field_name: fieldName,
                entry_index: entryIndex,
            });
            this.env.bus.trigger("KENSEI:TASK_DESC_TRIGGERED", {
                field_name: fieldName,
                entry_index: entryIndex,
            });
            this._pollDescriptionThenTriggerQc(recordId, fieldName, entryIndex);
        } catch (e) {
            console.warn("[kensei-dashboard] Auto task description trigger failed:", e);
        }
    }

    _pollDescriptionThenTriggerQc(recordId, fieldName, entryIndex) {
        const poll = setInterval(async () => {
            try {
                await this.props.record.load();
                const raw = this.props.record.data[fieldName];
                if (!raw || !raw.trim()) return;
                const parsed = JSON.parse(raw);
                const entries = Array.isArray(parsed) ? parsed : [parsed];
                if (entryIndex >= entries.length) return;
                const status = entries[entryIndex].task_description_status;
                if (!status || status === "pending") return;
                clearInterval(poll);
                if (status !== "done") return;
                const qcStatus = entries[entryIndex].qc_status;
                if (qcStatus === "pending" || qcStatus === "done") return;
                await rpc("/kensei/trajectory_qc", {
                    record_id: recordId,
                    field_name: fieldName,
                    entry_index: entryIndex,
                });
                this.env.bus.trigger("KENSEI:QC_TRIGGERED", {
                    field_name: fieldName,
                    entry_index: entryIndex,
                });
            } catch (e) {
                clearInterval(poll);
                console.warn("[kensei-dashboard] Auto QC trigger failed:", e);
            }
        }, 5000);
    }

    get hasAnySandboxRunning() {
        return Object.values(this.state.sandboxes).some(
            (sb) => sb.docker_status === "running" || sb.docker_status === "starting"
        );
    }

    async _checkGogAuthStatus() {
        if (!this.taskId) return;
        try {
            const data = await rpc("/kensei/gog/status", { task_id: this.taskId });
            this.state.gogAuthDone = !!data.authenticated;
        } catch (e) {
            console.warn("[kensei-dashboard] Failed to check gog auth status:", e);
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

    get trajectoryEntries() {
        const fieldName = TRAJECTORY_FIELD_MAP[this.state.activeTab];
        if (!fieldName) return [];
        const raw = this.props.record.data[fieldName];
        if (!raw || !raw.trim()) return [];
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed : [parsed];
        } catch (_e) {
            return [];
        }
    }

    get trajectoryCount() {
        return this.trajectoryEntries.length;
    }

    get maxTrajectories() {
        return 12;
    }

    _loadRubrics() {
        const raw = this.props.record.data.rubrics;
        if (!raw || !raw.trim()) {
            this.state.rubrics = [];
            return;
        }
        try {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) {
                // Legacy format: plain array — add justification to each if missing
                this.state.rubrics = parsed.map(r => ({
                    ...r,
                    justification: r.justification || { claude: "", glm: "" },
                }));
            } else if (parsed && typeof parsed === "object" && Array.isArray(parsed.rubrics)) {
                // Wrapped format migration — move global justification into each rubric
                const globalJ = parsed.justification || { claude: "", glm: "" };
                this.state.rubrics = parsed.rubrics.map(r => ({
                    ...r,
                    justification: r.justification || { ...globalJ },
                }));
            } else {
                this.state.rubrics = [];
            }
        } catch (_e) {
            this.state.rubrics = [];
        }
    }

    async _saveRubrics() {
        const value = JSON.stringify(this.state.rubrics);
        await this.orm.write("kensei.kensei", [this.taskId], { rubrics: value });
        await this.props.record.load();
    }

    onRubricLabelInput(ev) {
        this.state.newRubricLabel = ev.target.value;
    }

    async onAddRubric() {
        const label = this.state.newRubricLabel.trim();
        if (!label) return;
        const hasEmpty = this.state.rubrics.some(r =>
            r.claude.some(v => v === null) || r.glm.some(v => v === null)
        );
        if (hasEmpty) {
            this.state.rubricError = "Complete all Pass/Fail ratings on existing rubrics before adding a new one.";
            return;
        }
        this.state.rubricError = "";
        this.state.rubrics.push({
            label,
            claude: Array(12).fill(null),
            glm: Array(12).fill(null),
            justification: { claude: "", glm: "" },
        });
        this.state.newRubricLabel = "";
        await this._saveRubrics();
    }

    onRubricKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.onAddRubric();
        }
    }

    async onRemoveRubric(index) {
        this.state.rubrics.splice(index, 1);
        await this._saveRubrics();
    }

    async onToggleRubricResult(rubricIndex, model, slotIndex) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return;
        const current = rubric[model][slotIndex];
        if (current === null || current === "fail") {
            rubric[model][slotIndex] = "pass";
        } else {
            rubric[model][slotIndex] = "fail";
        }
        await this._saveRubrics();
    }

    rubricNeedsJustification(rubricIndex, model) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return false;
        const modelKey = model === "claude" ? "claude" : "glm";
        const pass8 = rubric[modelKey].slice(0, 8);
        return pass8.every(v => v === "fail");
    }

    onJustificationInput(rubricIndex, model, ev) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return;
        if (!rubric.justification) rubric.justification = { claude: "", glm: "" };
        rubric.justification[model] = ev.target.value;
    }

    async onSaveJustification() {
        await this._saveRubrics();
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
registry.category("view_widgets").add("kensei_task_dashboard", taskDashboardDef);
