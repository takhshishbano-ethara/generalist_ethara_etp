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
    { type: "gpt", label: "GPT-5.5", icon: "fa-bolt" },
];

const TRAJECTORY_FIELD_MAP = {
    claude: "claude_trajectory",
    glm: "glm_trajectory",
    gpt: "gpt_trajectory",
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
            testResults: {},
            expandedTestIds: {},
            testWeightsStatus: "idle",
            testWeightsError: "",
        });

        this._onSandboxStatusChanged = (ev) => {
            this._handleSandboxStatusChanged(ev.detail);
        };
        this.env.bus.addEventListener(
            "KENSEI:SANDBOX_STATUS_CHANGED",
            this._onSandboxStatusChanged,
        );

        onMounted(async () => {
            await this._loadSandboxes();
            this._checkGogAuthStatus();
            this._loadRubrics();
            this._loadTestResults();
            this._loadTestWeightsStatus();
        });
        onWillUnmount(() => {
            this._stopPolling();
            if (this._testWeightsPollTimer) clearInterval(this._testWeightsPollTimer);
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
        await this._loadTestResults();
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
            await this._loadTestResults();
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

    getModelTrajectoryCount(modelType) {
        const fieldName = TRAJECTORY_FIELD_MAP[modelType];
        if (!fieldName) return 0;
        const raw = this.props.record.data[fieldName];
        if (!raw || !raw.trim()) return 0;
        try {
            const parsed = JSON.parse(raw);
            return Array.isArray(parsed) ? parsed.length : 0;
        } catch (_e) {
            return 0;
        }
    }

    _loadRubrics() {
        const raw = this.props.record.data.rubrics;
        if (!raw || !raw.trim()) {
            this.state.rubrics = [];
            return;
        }
        try {
            const parsed = JSON.parse(raw);
            const migrateRubric = (r) => ({
                ...r,
                gpt: r.gpt || Array(12).fill(null),
                justification: r.justification || { claude: "", glm: "", gpt: "" },
                score: r.score ?? 1,
                is_positive: r.is_positive ?? true,
                type: r.type || "task completion",
                evaluation_target: r.evaluation_target || "state change",
                importance: r.importance || "important",
            });
            if (Array.isArray(parsed)) {
                this.state.rubrics = parsed.map(migrateRubric);
            } else if (parsed && typeof parsed === "object" && Array.isArray(parsed.rubrics)) {
                const globalJ = parsed.justification || { claude: "", glm: "", gpt: "" };
                this.state.rubrics = parsed.rubrics.map(r => migrateRubric({
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

    async _loadTestResults() {
        if (!this.taskId) return;
        try {
            const sandboxIds = Object.values(this.state.sandboxes).map(sb => sb.id).filter(Boolean);
            let domain;
            if (sandboxIds.length > 0) {
                domain = ["|", ["kensei_id", "=", this.taskId], ["sandbox_id", "in", sandboxIds]];
            } else {
                domain = [["kensei_id", "=", this.taskId]];
            }
            const results = await this.orm.searchRead(
                "kensei.test.result",
                domain,
                [
                    "id", "sandbox_id", "model_type", "model_used", "status",
                    "tests_total", "tests_passed", "tests_failed", "tests_errored",
                    "duration_generation_ms", "duration_execution_ms", "create_date",
                    "trajectory_index", "test_code", "test_output", "score", "test_scores",
                ],
                { order: "trajectory_index asc, create_date desc", limit: 50 },
            );
            const grouped = {};
            for (const r of results) {
                let modelType = r.model_type || "";
                if (!modelType && r.sandbox_id) {
                    const sbId = r.sandbox_id[0];
                    const sb = Object.values(this.state.sandboxes).find(s => s.id === sbId);
                    modelType = sb ? sb.model_type : "unknown";
                }
                if (!modelType) modelType = "unknown";
                if (!grouped[modelType]) grouped[modelType] = [];
                grouped[modelType].push(r);
            }
            this.state.testResults = grouped;
        } catch (e) {
            console.warn("[kensei-dashboard] Failed to load test results:", e);
        }
    }

    async _loadTestWeightsStatus() {
        if (!this.taskId) return;
        try {
            const [data] = await this.orm.read(
                "kensei.kensei",
                [this.taskId],
                ["test_weights_status", "test_weights_error"],
            );
            this.state.testWeightsStatus = data.test_weights_status || "idle";
            this.state.testWeightsError = data.test_weights_error || "";
        } catch (e) {
            console.warn("[kensei-dashboard] Failed to load test weights status:", e);
        }
    }

    async onGenerateTestWeights() {
        if (!this.taskId) return;
        try {
            this.state.testWeightsStatus = "generating";
            this.state.testWeightsError = "";
            await this.orm.call("kensei.kensei", "action_generate_test_weights", [[this.taskId]]);
            this.notification.add("Test weight generation started.", { type: "info" });
            this._pollTestWeightsStatus();
        } catch (e) {
            this.state.testWeightsStatus = "error";
            this.state.testWeightsError = e.message || String(e);
            this.notification.add("Failed to start test weight generation: " + (e.message || e), { type: "danger" });
        }
    }

    _pollTestWeightsStatus() {
        if (this._testWeightsPollTimer) clearInterval(this._testWeightsPollTimer);
        this._testWeightsPollTimer = setInterval(async () => {
            await this._loadTestWeightsStatus();
            if (this.state.testWeightsStatus !== "generating") {
                clearInterval(this._testWeightsPollTimer);
                this._testWeightsPollTimer = null;
                if (this.state.testWeightsStatus === "done") {
                    this.notification.add("Test weights generated successfully.", { type: "success" });
                } else if (this.state.testWeightsStatus === "error") {
                    this.notification.add("Test weight generation failed: " + this.state.testWeightsError, { type: "danger" });
                }
            }
        }, 5000);
    }

    get activeTestResults() {
        return this.state.testResults[this.state.activeTab] || [];
    }

    get testResultsByTrajectory() {
        const results = this.activeTestResults;
        const grouped = {};
        for (const r of results) {
            const idx = r.trajectory_index || 0;
            if (!grouped[idx]) grouped[idx] = [];
            grouped[idx].push(r);
        }
        return Object.keys(grouped)
            .sort((a, b) => Number(a) - Number(b))
            .map(idx => ({ index: Number(idx), results: grouped[idx] }));
    }

    getTestResultsForTrajectory(trajIndex) {
        return this.activeTestResults.filter(r => r.trajectory_index === trajIndex);
    }

    onToggleTestDetail(resultId) {
        this.state.expandedTestIds = {
            ...this.state.expandedTestIds,
            [resultId]: !this.state.expandedTestIds[resultId],
        };
    }

    isTestExpanded(resultId) {
        return !!this.state.expandedTestIds[resultId];
    }

    async onSetTestScore(resultId, score) {
        try {
            await this.orm.write("kensei.test.result", [resultId], { score });
            const allResults = Object.values(this.state.testResults).flat();
            const result = allResults.find(r => r.id === resultId);
            if (result) result.score = score;
        } catch (e) {
            this.notification.add("Failed to save score", { type: "danger" });
        }
    }

    async onSetFunctionScore(resultId, funcName, score) {
        const allResults = Object.values(this.state.testResults).flat();
        const result = allResults.find(r => r.id === resultId);
        if (!result) return;
        let scores = {};
        try {
            scores = JSON.parse(result.test_scores || "{}");
        } catch (_e) {
            scores = {};
        }
        scores[funcName] = score;
        const value = JSON.stringify(scores);
        try {
            await this.orm.write("kensei.test.result", [resultId], { test_scores: value });
            result.test_scores = value;
        } catch (e) {
            this.notification.add("Failed to save function score", { type: "danger" });
        }
    }

    getFunctionScore(result, funcName) {
        try {
            const scores = JSON.parse(result.test_scores || "{}");
            return scores[funcName] ?? null;
        } catch (_e) {
            return null;
        }
    }

    getTestFunctions(result) {
        if (!result.test_code) return [];
        const regex = /^(?:def|async def)\s+(test_\w+)\s*\(/gm;
        const funcs = [];
        let match;
        while ((match = regex.exec(result.test_code)) !== null) {
            funcs.push(match[1]);
        }
        return funcs;
    }

    getFunctionStatus(result, funcName) {
        if (!result.test_output) return "unknown";
        if (result.test_output.includes(funcName + " PASSED")) return "passed";
        if (result.test_output.includes(funcName + " FAILED")) return "failed";
        if (result.test_output.includes(funcName + " ERROR")) return "error";
        return "unknown";
    }

    getFunctionOutput(result, funcName) {
        if (!result.test_output) return "";
        const output = result.test_output;
        const patterns = [
            new RegExp(`_{2,}\\s*${funcName}\\s*_{2,}([\\s\\S]*?)(?=_{2,}\\s*\\w|={2,}|$)`, "m"),
            new RegExp(`FAILED.*${funcName}[^\\n]*\\n([\\s\\S]*?)(?=FAILED|PASSED|ERROR|={2,}|$)`, "m"),
            new RegExp(`(${funcName}[\\s\\S]*?)(?=\\n\\S+::\\w+|\\n={2,}|$)`, "m"),
        ];
        for (const pat of patterns) {
            const match = output.match(pat);
            if (match && match[1] && match[1].trim()) return match[1].trim();
        }
        const lines = output.split("\n");
        const relevant = [];
        let capturing = false;
        for (const line of lines) {
            if (line.includes(funcName)) {
                capturing = true;
                relevant.push(line);
            } else if (capturing) {
                if (line.match(/^(PASSED|FAILED|ERROR|_{2,}|={2,})/)) break;
                relevant.push(line);
            }
        }
        return relevant.join("\n").trim();
    }

    getFunctionCode(result, funcName) {
        if (!result.test_code) return "";
        const code = result.test_code;
        const regex = new RegExp(`((?:def|async def)\\s+${funcName}\\s*\\([\\s\\S]*?)(?=\\n(?:def|async def|class)\\s|$)`, "m");
        const match = code.match(regex);
        return match ? match[1].trim() : "";
    }

    get latestTestResult() {
        const results = this.activeTestResults;
        return results.length > 0 ? results[0] : null;
    }

    get testResultsSummary() {
        const results = this.activeTestResults;
        if (results.length === 0) return null;
        const total = results.length;
        const passed = results.filter(r => r.status === "passed").length;
        const failed = results.filter(r => r.status === "failed").length;
        const errored = results.filter(r => r.status === "error").length;
        const running = results.filter(r => r.status === "running" || r.status === "generating").length;
        return { total, passed, failed, errored, running };
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
            r.claude.some(v => v === null) || r.glm.some(v => v === null) || r.gpt.some(v => v === null)
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
            gpt: Array(12).fill(null),
            justification: { claude: "", glm: "", gpt: "" },
            score: 1,
            is_positive: true,
            type: "task completion",
            evaluation_target: "state change",
            importance: "important",
        });
        this.state.newRubricLabel = "";
        await this._saveRubrics();
    }

    async onRubricMetaChange(rubricIndex, field, ev) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return;
        const val = ev.target.value;
        if (field === "score") {
            rubric.score = parseInt(val, 10);
            rubric.is_positive = rubric.score > 0;
        } else {
            rubric[field] = val;
        }
        if (field === "importance") {
            const absScore = Math.abs(rubric.score);
            if (rubric.importance === "critically_important" && absScore < 5) {
                rubric.score = rubric.is_positive ? 5 : -5;
            }
        }
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
        const modelKey = model;
        const pass8 = rubric[modelKey].slice(0, 8);
        return pass8.every(v => v === "fail");
    }

    onJustificationInput(rubricIndex, model, ev) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return;
        if (!rubric.justification) rubric.justification = { claude: "", glm: "", gpt: "" };
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
