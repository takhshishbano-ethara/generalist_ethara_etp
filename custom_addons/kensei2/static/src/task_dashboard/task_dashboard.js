/** @odoo-module */
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";
import { GogAuthDialog } from "../components/gog_auth_dialog/gog_auth_dialog";
import { SelectivePromptDialog } from "../components/selective_prompt_dialog/selective_prompt_dialog";
import { Kensei2ChatWidget } from "../chat_widget/chat_widget";
import { rpc } from "@web/core/network/rpc";

const BATCH_MODELS = [
    { type: "claude", label: "Claude Opus 4.7", color: "#c084fc" },
    { type: "gpt", label: "GPT-5.5", color: "#34d399" },
];

const TRAJECTORY_FIELD_MAP = {
    claude: "claude_trajectory",
    gpt: "gpt_trajectory",
};

const BATCH_POLL_INTERVAL_MS = 5000;

const MAX_BATCH_ATTACHMENTS = 10;
const MAX_BATCH_ATTACHMENT_SIZE_MB = 75;
const MAX_BATCH_ATTACHMENT_SIZE_BYTES = MAX_BATCH_ATTACHMENT_SIZE_MB * 1024 * 1024;
const ALLOWED_IMAGE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp"];
const ALLOWED_DOC_TYPES = [
    "application/pdf", "text/plain", "text/markdown",
    "text/html", "text/csv", "application/json",
    "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
];
const ALLOWED_VIDEO_TYPES = [
    "video/mp4", "video/webm", "video/quicktime",
    "video/x-msvideo", "video/mpeg", "video/x-m4v",
];
const ALLOWED_AUDIO_TYPES = [
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/ogg",
    "audio/webm", "audio/x-m4a", "audio/mp4",
];
const ALLOWED_ATTACHMENT_TYPES = [
    ...ALLOWED_IMAGE_TYPES, ...ALLOWED_DOC_TYPES,
    ...ALLOWED_VIDEO_TYPES, ...ALLOWED_AUDIO_TYPES,
];
const EXT_MIME_MAP = {
    doc: "application/msword",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    md: "text/markdown", pdf: "application/pdf",
    png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg",
    gif: "image/gif", webp: "image/webp",
    mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime",
    mp3: "audio/mpeg", wav: "audio/wav", ogg: "audio/ogg", m4a: "audio/mp4",
    txt: "text/plain", html: "text/html", csv: "text/csv",
    json: "application/json", jsonl: "application/json",
};

export class TaskDashboard extends Component {
    static template = "kensei2.TaskDashboard";
    static components = { GogAuthDialog, SelectivePromptDialog, Kensei2ChatWidget };
    static props = { ...standardWidgetProps };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        this.batchModels = BATCH_MODELS;
        this._pollTimer = null;

        this.state = useState({
            batchPrompt: "",
            batchStarting: false,
            batchStopping: false,
            pendingAttachments: [],
            attachmentError: "",
            dragOver: false,
            sandboxes: [],
            showGogAuth: false,
            gogAuthDone: false,
            rubrics: [],
            newRubricLabel: "",
            rubricError: "",
            testResults: {},
            expandedTestIds: {},
            testWeightsStatus: "idle",
            testWeightsError: "",
            activeTrajectoryTab: "claude",
            pendingPodActions: {},
            selectedSandboxId: null,
            harborExporting: false,
            showSelectivePrompt: false,
            selectivePromptInFlight: false,
            rubricEvalStatus: "idle",
            rubricEvalError: "",
            rubricEvalCompleted: 0,
            rubricEvalTotal: 0,
            rubricOverlapStatus: "idle",
            rubricOverlapError: "",
            rubricOverlapReport: "",
            showRubricOverlapReport: true,
            goldenStreamStatus: "idle",
            goldenStreamError: "",
            goldenStreamText: "",
        });
        this._rubricEvalAbort = null;
        this._rubricOverlapAbort = null;
        this._goldenStreamAbort = null;

        this._onBatchStatusChanged = (ev) => {
            this._handleBatchStatusChanged(ev.detail);
        };
        this.env.bus.addEventListener(
            "KENSEI2:BATCH_STATUS_CHANGED",
            this._onBatchStatusChanged,
        );
        this._onSandboxStatusChanged = (ev) => {
            this._handleSandboxStatusChanged(ev.detail);
        };
        this.env.bus.addEventListener(
            "KENSEI2:SANDBOX_STATUS_CHANGED",
            this._onSandboxStatusChanged,
        );
        this._onSelectivePromptDone = (ev) => {
            this._handleSelectivePromptDone(ev.detail);
        };
        this.env.bus.addEventListener(
            "KENSEI2:SELECTIVE_PROMPT_DONE",
            this._onSelectivePromptDone,
        );

        onMounted(async () => {
            this._checkGogAuthStatus();
            this._loadRubrics();
            this._loadTestWeightsStatus();
            this._loadStreamingStatuses();
            await this._loadSandboxes();
            this._loadTestResults();
            if (this.batchStatus === "starting" || this.batchStatus === "ready" || this.batchStatus === "running" || this.batchStatus === "stopping") {
                this._startPolling();
            }
        });
        onWillUnmount(() => {
            this._stopPolling();
            if (this._testWeightsPollTimer) clearInterval(this._testWeightsPollTimer);
            if (this._rubricEvalAbort) this._rubricEvalAbort.abort();
            if (this._rubricOverlapAbort) this._rubricOverlapAbort.abort();
            if (this._goldenStreamAbort) this._goldenStreamAbort.abort();
            this.env.bus.removeEventListener(
                "KENSEI2:BATCH_STATUS_CHANGED",
                this._onBatchStatusChanged,
            );
            this.env.bus.removeEventListener(
                "KENSEI2:SANDBOX_STATUS_CHANGED",
                this._onSandboxStatusChanged,
            );
            this.env.bus.removeEventListener(
                "KENSEI2:SELECTIVE_PROMPT_DONE",
                this._onSelectivePromptDone,
            );
        });
    }

    async _handleBatchStatusChanged(_payload) {
        await this.props.record.load();
        await this._loadSandboxes();
        await this._loadTestResults();
        const status = this.batchStatus;
        if (status === "ready") {
            this.state.batchStarting = false;
            this.notification.add("All pods are ready. Enter a prompt and click Send.", { type: "success" });
        }
        if (status === "done" || status === "error" || status === "idle") {
            this._stopPolling();
            this.state.batchStarting = false;
            this.state.batchStopping = false;
        }
    }

    async _handleSandboxStatusChanged(_payload) {
        await this._loadSandboxes();
        await this.props.record.load();
        if (this.state.selectedSandboxId) {
            const sb = this.state.sandboxes.find(
                (s) => s.id === this.state.selectedSandboxId
            );
            if (!sb || sb.docker_status === "stopped") {
                this.state.selectedSandboxId = null;
            }
        }
    }

    async _handleSelectivePromptDone(payload) {
        this.state.selectivePromptInFlight = false;
        await this.props.record.load();
        await this._loadSandboxes();
        await this._loadTestResults();
        if (this.state.selectedSandboxId) {
            const sb = this.state.sandboxes.find(
                (s) => s.id === this.state.selectedSandboxId
            );
            if (!sb || sb.docker_status === "stopped") {
                this.state.selectedSandboxId = null;
            }
        }
        if (!this.isBatchActive) {
            this._stopPolling();
        }
        const completed = payload?.completed || 0;
        const failed = payload?.failed || 0;
        const stopErrors = (payload?.stop_errors || []).length;
        if (failed > 0 || stopErrors > 0) {
            this.notification.add(
                `Selective prompt finished: ${completed} completed, ${failed} failed.`,
                { type: "warning" },
            );
        } else {
            this.notification.add(
                `Selective prompt finished on ${completed} pod(s). Trajectories updated.`,
                { type: "success" },
            );
        }
    }

    get taskId() {
        return this.props.record.resId;
    }

    get taskEmail() {
        return this.props.record.data.email || "";
    }

    get batchStatus() {
        return this.props.record.data.batch_status || "idle";
    }

    get batchError() {
        return this.props.record.data.batch_error || "";
    }

    get batchSize() {
        return this.props.record.data.batch_size || 8;
    }

    get isBatchActive() {
        const s = this.batchStatus;
        return s === "starting" || s === "ready" || s === "running" || s === "stopping";
    }

    get canStartBatch() {
        return !this.isBatchActive && !this.state.batchStarting && this.taskId;
    }

    get canSendPrompt() {
        return this.batchStatus === "ready" && !this.state.batchStarting;
    }

    get canSendSelectivePrompt() {
        return this.taskId && this.runningCount > 0;
    }

    get selectivePromptPods() {
        return this.state.sandboxes
            .filter((sb) => sb.docker_status === "running")
            .map((sb) => ({
                id: sb.id,
                modelLabel: this.podModelLabel(sb),
                sessionLabel: this.podSessionLabel(sb),
                color: (BATCH_MODELS.find((m) => m.type === sb.model_type) || {}).color || "#94a3b8",
            }));
    }

    get canStopBatch() {
        return (this.batchStatus === "starting" || this.batchStatus === "ready" || this.batchStatus === "running") && !this.state.batchStopping;
    }

    get totalPods() {
        return BATCH_MODELS.length * this.batchSize;
    }

    get sandboxesByModel() {
        const grouped = {};
        for (const m of BATCH_MODELS) {
            grouped[m.type] = this.state.sandboxes
                .filter((sb) => sb.model_type === m.type)
                .sort((a, b) => (a.variant_index || 0) - (b.variant_index || 0));
        }
        return grouped;
    }

    get completedCount() {
        return this.state.sandboxes.filter(
            (sb) => sb.session_status === "completed"
        ).length;
    }

    get runningCount() {
        return this.state.sandboxes.filter(
            (sb) => sb.docker_status === "running"
        ).length;
    }

    get startingCount() {
        return this.state.sandboxes.filter(
            (sb) => sb.docker_status === "starting"
        ).length;
    }

    get errorCount() {
        return this.state.sandboxes.filter(
            (sb) => sb.docker_status === "error"
        ).length;
    }

    get stoppedCount() {
        return this.state.sandboxes.filter(
            (sb) => sb.docker_status === "stopped"
        ).length;
    }

    get batchProgressPercent() {
        const total = this.totalPods;
        if (total === 0) return 0;
        return Math.round((this.completedCount / total) * 100);
    }

    async _loadSandboxes() {
        if (!this.taskId) return;

        const sandboxes = await this.orm.searchRead(
            "kensei2.sandbox",
            [["kensei2_id", "=", this.taskId]],
            [
                "id", "model_type", "variant_index", "docker_status",
                "docker_port", "docker_gateway_token",
                "docker_ws_url", "docker_error", "docker_workdir",
                "session_status", "docker_compose_project",
            ],
            { order: "model_type asc, variant_index asc" },
        );

        this.state.sandboxes = sandboxes;
    }

    _startPolling() {
        if (this._pollTimer) return;
        this._pollTimer = setInterval(() => this._pollBatchStatus(), BATCH_POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this._pollTimer) {
            clearInterval(this._pollTimer);
            this._pollTimer = null;
        }
    }

    async _pollBatchStatus() {
        try {
            await this.props.record.load();
            await this._loadSandboxes();
            const status = this.batchStatus;
            if (status === "ready" && this.state.batchStarting) {
                this.state.batchStarting = false;
                this.notification.add("All pods are ready. Enter a prompt and click Send.", { type: "success" });
            }
            if (status === "done" || status === "error" || status === "idle") {
                if (!this.state.selectivePromptInFlight) {
                    this._stopPolling();
                }
                this.state.batchStarting = false;
                this.state.batchStopping = false;
                if (status === "done") {
                    this.notification.add("Batch completed successfully!", { type: "success" });
                    await this._loadTestResults();
                } else if (status === "error") {
                    this.notification.add("Batch completed with errors.", { type: "warning" });
                    await this._loadTestResults();
                }
            }
        } catch (e) {
            console.warn("[kensei2-dashboard] Batch poll failed:", e);
        }
    }

    onBatchPromptInput(ev) {
        this.state.batchPrompt = ev.target.value;
    }

    get acceptedFileTypes() {
        return ALLOWED_ATTACHMENT_TYPES.join(",");
    }

    get hasAttachments() {
        return this.state.pendingAttachments.length > 0;
    }

    onAttachmentPick() {
        const input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.accept = this.acceptedFileTypes;
        input.onchange = (ev) => {
            if (ev.target.files?.length) this._processFiles(ev.target.files);
        };
        input.click();
    }

    onAttachmentDragOver(ev) {
        ev.preventDefault();
        this.state.dragOver = true;
    }

    onAttachmentDragLeave() {
        this.state.dragOver = false;
    }

    onAttachmentDrop(ev) {
        ev.preventDefault();
        this.state.dragOver = false;
        const files = ev.dataTransfer?.files;
        if (files?.length) this._processFiles(files);
    }

    onRemoveAttachment(index) {
        const att = this.state.pendingAttachments[index];
        if (att?.previewUrl) URL.revokeObjectURL(att.previewUrl);
        this.state.pendingAttachments.splice(index, 1);
        this.state.attachmentError = "";
    }

    formatFileSize(bytes) {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }

    _processFiles(fileList) {
        this.state.attachmentError = "";
        const files = Array.from(fileList);

        if (this.state.pendingAttachments.length + files.length > MAX_BATCH_ATTACHMENTS) {
            this.state.attachmentError = `Maximum ${MAX_BATCH_ATTACHMENTS} attachments allowed.`;
            return;
        }

        for (const file of files) {
            let mimeType = file.type;
            if (!mimeType || mimeType === "application/octet-stream") {
                const ext = file.name.split(".").pop().toLowerCase();
                mimeType = EXT_MIME_MAP[ext] || mimeType;
            }
            if (!ALLOWED_ATTACHMENT_TYPES.includes(mimeType)) {
                this.state.attachmentError = `"${file.name}" has unsupported type: ${mimeType || "unknown"}`;
                continue;
            }
            const totalWithNew = this.state.pendingAttachments.reduce((s, a) => s + a.size, 0) + file.size;
            if (totalWithNew > MAX_BATCH_ATTACHMENT_SIZE_BYTES) {
                this.state.attachmentError = `Total size exceeds ${MAX_BATCH_ATTACHMENT_SIZE_MB} MB limit.`;
                continue;
            }
            const isImage = ALLOWED_IMAGE_TYPES.includes(mimeType);
            const isVideo = ALLOWED_VIDEO_TYPES.includes(mimeType);
            const previewUrl = (isImage || isVideo) ? URL.createObjectURL(file) : null;
            this.state.pendingAttachments.push({
                id: crypto.randomUUID(),
                file,
                name: file.name,
                mimeType,
                size: file.size,
                previewUrl,
                isImage,
                isVideo,
            });
        }
    }

    _fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const dataUri = reader.result;
                const commaIdx = dataUri.indexOf(",");
                resolve(commaIdx >= 0 ? dataUri.slice(commaIdx + 1) : dataUri);
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    async _uploadAttachments() {
        const ids = [];
        for (const att of this.state.pendingAttachments) {
            const rawBase64 = await this._fileToBase64(att.file);
            const result = await this.orm.call("kensei2.kensei2", "upload_batch_attachment", [
                [this.taskId], att.name, rawBase64, att.mimeType,
            ]);
            if (result.error) throw new Error(result.error);
            ids.push(result.attachment_id);
        }
        return ids;
    }

    async onStartBatch() {
        if (!this.canStartBatch) return;

        this.state.batchStarting = true;
        try {
            await this.orm.call(
                "kensei2.kensei2", "action_start_batch",
                [[this.taskId]],
            );
            await this.props.record.load();
            await this._loadSandboxes();
            this._startPolling();
            this.notification.add(`Deploying ${this.totalPods} pods...`, { type: "info" });
        } catch (e) {
            this.state.batchStarting = false;
            const msg = e.data?.message || e.message || "Failed to start batch";
            this.notification.add(msg, { type: "danger" });
        }
    }

    async onSendBatchPrompt() {
        const prompt = this.state.batchPrompt.trim();
        if (!prompt && !this.hasAttachments) {
            this.notification.add("Please enter a prompt or attach files.", { type: "warning" });
            return;
        }
        if (!this.canSendPrompt) return;

        this.state.batchStarting = true;
        this.state.attachmentError = "";
        try {
            let attachmentIds = [];
            if (this.hasAttachments) {
                attachmentIds = await this._uploadAttachments();
            }
            await this.orm.call(
                "kensei2.kensei2", "action_send_batch_prompt",
                [[this.taskId], prompt || "", attachmentIds],
            );
            this.state.pendingAttachments = [];
            await this.props.record.load();
            await this._loadSandboxes();
            this.notification.add("Prompt sent to all pods.", { type: "info" });
        } catch (e) {
            this.state.batchStarting = false;
            const msg = e.data?.message || e.message || "Failed to send prompt";
            this.notification.add(msg, { type: "danger" });
        }
    }

    onOpenSelectivePrompt() {
        if (!this.canSendSelectivePrompt) {
            this.notification.add("No running pods to send a prompt to.", { type: "warning" });
            return;
        }
        this.state.showSelectivePrompt = true;
    }

    onCloseSelectivePrompt() {
        this.state.showSelectivePrompt = false;
    }

    async onSendSelectivePrompt({ prompt, sandboxIds }) {
        if (!sandboxIds || sandboxIds.length === 0) {
            this.notification.add("Select at least one pod.", { type: "warning" });
            return;
        }
        if (!prompt && !this.hasAttachments) {
            this.notification.add("Enter a prompt or attach files.", { type: "warning" });
            return;
        }
        let attachmentIds = [];
        if (this.hasAttachments) {
            attachmentIds = await this._uploadAttachments();
        }
        await this.orm.call(
            "kensei2.kensei2", "action_send_selective_prompt",
            [[this.taskId], prompt || "", sandboxIds, attachmentIds],
        );
        this.state.pendingAttachments = [];
        this.state.showSelectivePrompt = false;
        this.state.selectivePromptInFlight = true;
        this._startPolling();
        await this._loadSandboxes();
        this.notification.add(
            `Prompt sent to ${sandboxIds.length} pod(s). Will auto-stop and export trajectory when done.`,
            { type: "info" },
        );
    }

    async onStopBatch() {
        if (!this.canStopBatch) return;

        const confirmed = window.confirm(
            "Are you sure you want to stop the batch? All running pods will be terminated and trajectories exported."
        );
        if (!confirmed) return;

        this.state.batchStopping = true;
        try {
            await this.orm.call("kensei2.kensei2", "action_stop_batch", [[this.taskId]]);
            await this.props.record.load();
            await this._loadSandboxes();
            this.notification.add("Batch stopping...", { type: "info" });
        } catch (e) {
            this.state.batchStopping = false;
            const msg = e.data?.message || e.message || "Failed to stop batch";
            this.notification.add(msg, { type: "danger" });
        }
    }

    get canExportHarbor() {
        return this.taskId && (this.batchStatus === "done" || this.batchStatus === "error");
    }

    async onExportToHarbor() {
        if (!this.canExportHarbor) return;
        this.state.harborExporting = true;
        try {
            await this.orm.call("kensei2.kensei2", "action_export_to_harbor", [[this.taskId]]);
            this.notification.add("Harbor export started. Files will be uploaded to S3.", { type: "info" });
        } catch (e) {
            const msg = e.data?.message || e.message || "Export failed";
            this.notification.add(msg, { type: "danger" });
        } finally {
            this.state.harborExporting = false;
        }
    }

    onDownloadHarbor() {
        if (!this.taskId) return;
        window.open(`/kensei2/harbor/download/${this.taskId}`, "_blank");
    }

    podStatusClass(sandbox) {
        const ds = sandbox.docker_status || "stopped";
        const ss = sandbox.session_status || "not_started";
        if (ss === "completed") return "completed";
        if (ds === "running") return "running";
        if (ds === "starting") return "starting";
        if (ds === "error") return "error";
        return "stopped";
    }

    podStatusIcon(sandbox) {
        const cls = this.podStatusClass(sandbox);
        const map = {
            completed: "fa-check",
            running: "fa-circle-o-notch fa-spin",
            starting: "fa-spinner fa-spin",
            error: "fa-exclamation-triangle",
            stopped: "fa-circle-o",
        };
        return map[cls] || "fa-circle-o";
    }

    podTooltip(sandbox) {
        const vi = sandbox.variant_index || 0;
        const modelLabel = this.podModelLabel(sandbox);
        const ds = sandbox.docker_status || "stopped";
        const ss = sandbox.session_status || "not_started";
        let tip = `${modelLabel} — Session ${vi} — Pod: ${ds}`;
        if (ss !== "not_started") tip += `, Session: ${ss}`;
        if (sandbox.docker_error) tip += `\nError: ${sandbox.docker_error}`;
        if (this.canRetryPod(sandbox)) tip += "\nClick refresh icon to retry this pod.";
        if (this.canStopPod(sandbox)) tip += "\nClick stop icon to stop this pod.";
        return tip;
    }

    podModelLabel(sandbox) {
        const model = BATCH_MODELS.find((m) => m.type === sandbox.model_type);
        return model ? model.label : (sandbox.model_type || "Unknown");
    }

    podSessionLabel(sandbox) {
        return `Session ${sandbox.variant_index || 0}`;
    }

    canRetryPod(sandbox) {
        const ds = sandbox.docker_status || "stopped";
        return ds === "error" || ds === "stopped";
    }

    canStopPod(sandbox) {
        const ds = sandbox.docker_status || "stopped";
        return ds === "starting" || ds === "running";
    }

    isPodActionPending(sandbox) {
        return Boolean(this.state.pendingPodActions[sandbox.id]);
    }

    podRetryIcon(sandbox) {
        const ds = sandbox.docker_status || "stopped";
        return ds === "error" ? "fa-refresh" : "fa-play";
    }

    podRetryLabel(sandbox) {
        const ds = sandbox.docker_status || "stopped";
        return ds === "error" ? "Retry" : "Start";
    }

    get selectedSandbox() {
        if (!this.state.selectedSandboxId) return null;
        return this.state.sandboxes.find(
            (sb) => sb.id === this.state.selectedSandboxId
        ) || null;
    }

    isSelectedSandbox(sandbox) {
        return sandbox.id === this.state.selectedSandboxId;
    }

    onSelectSandbox(sandbox) {
        if (this.state.selectedSandboxId === sandbox.id) {
            this.state.selectedSandboxId = null;
            return;
        }
        const ds = sandbox.docker_status || "stopped";
        if (ds !== "running" && ds !== "starting") {
            return;
        }
        this.state.selectedSandboxId = sandbox.id;
    }

    onCloseChatPanel() {
        this.state.selectedSandboxId = null;
    }

    async onRetryPod(sandbox) {
        if (!this.canRetryPod(sandbox) || this.isPodActionPending(sandbox)) {
            return;
        }
        const label = `${this.podModelLabel(sandbox)} ${this.podSessionLabel(sandbox)}`;
        const verb = this.podRetryLabel(sandbox);
        this.state.pendingPodActions[sandbox.id] = "retry";
        try {
            await this.orm.call("kensei2.sandbox", "action_retry_pod", [[sandbox.id]]);
            this.notification.add(`${verb}ing ${label}…`, { type: "info" });
            await this._loadSandboxes();
            this._startPolling();
        } catch (e) {
            const msg = e.data?.message || e.message || `Failed to ${verb.toLowerCase()} ${label}`;
            this.notification.add(msg, { type: "danger" });
        } finally {
            delete this.state.pendingPodActions[sandbox.id];
        }
    }

    async onStopPod(sandbox) {
        if (!this.canStopPod(sandbox) || this.isPodActionPending(sandbox)) {
            return;
        }
        const label = `${this.podModelLabel(sandbox)} ${this.podSessionLabel(sandbox)}`;
        if (!window.confirm(`Stop ${label}? Any in-progress work on this pod will be lost.`)) {
            return;
        }
        this.state.pendingPodActions[sandbox.id] = "stop";
        try {
            await this.orm.call("kensei2.sandbox", "action_stop_sandbox", [[sandbox.id]]);
            this.notification.add(`Stopping ${label}…`, { type: "info" });
            await this._loadSandboxes();
            this._startPolling();
        } catch (e) {
            const msg = e.data?.message || e.message || `Failed to stop ${label}`;
            this.notification.add(msg, { type: "danger" });
        } finally {
            delete this.state.pendingPodActions[sandbox.id];
        }
    }

    onTrajectoryTabClick(modelType) {
        this.state.activeTrajectoryTab = modelType;
    }

    get trajectoryEntries() {
        const fieldName = TRAJECTORY_FIELD_MAP[this.state.activeTrajectoryTab];
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

    _trajectoryCountCache = {};

    getModelTrajectoryCount(modelType) {
        const fieldName = TRAJECTORY_FIELD_MAP[modelType];
        if (!fieldName) return 0;
        const raw = this.props.record.data[fieldName];
        if (!raw || !raw.trim()) return 0;
        const cached = this._trajectoryCountCache[modelType];
        if (cached && cached.raw === raw) return cached.count;
        try {
            const parsed = JSON.parse(raw);
            const count = Array.isArray(parsed) ? parsed.length : 0;
            this._trajectoryCountCache[modelType] = { raw, count };
            return count;
        } catch (_e) {
            this._trajectoryCountCache[modelType] = { raw, count: 0 };
            return 0;
        }
    }

    getTestResultsForTrajectory(trajIndex) {
        const results = this.state.testResults[this.state.activeTrajectoryTab] || [];
        return results.filter(r => r.trajectory_index === trajIndex);
    }

    async _checkGogAuthStatus() {
        if (!this.taskId) return;
        try {
            const data = await rpc("/kensei2/gog/status", { task_id: this.taskId });
            this.state.gogAuthDone = !!data.authenticated;
        } catch (e) {
            console.warn("[kensei2-dashboard] Failed to check gog auth status:", e);
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
                claude: r.claude || Array(12).fill(null),
                gpt: r.gpt || Array(12).fill(null),
                justification: r.justification || { claude: "", gpt: "" },
                score: r.score ?? 1,
                is_positive: r.is_positive ?? true,
                type: r.type || "task completion",
                evaluation_target: r.evaluation_target || "state change",
                importance: r.importance || "important",
            });
            if (Array.isArray(parsed)) {
                this.state.rubrics = parsed.map(migrateRubric);
            } else if (parsed && typeof parsed === "object" && Array.isArray(parsed.rubrics)) {
                const globalJ = parsed.justification || { claude: "", gpt: "" };
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

    async _saveRubrics() {
        const value = JSON.stringify(this.state.rubrics);
        await this.orm.write("kensei2.kensei2", [this.taskId], { rubrics: value });
        await this.props.record.load();
    }

    onRubricLabelInput(ev) {
        this.state.newRubricLabel = ev.target.value;
    }

    async onAddRubric() {
        const label = this.state.newRubricLabel.trim();
        if (!label) return;
        this.state.rubrics.push({
            label,
            claude: Array(12).fill(null),
            gpt: Array(12).fill(null),
            justification: { claude: "", gpt: "" },
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

    async onUploadRubricsFile(ev) {
        const input = ev.target;
        const file = input.files && input.files[0];
        if (!file) return;
        this.state.rubricError = "";
        try {
            const text = await file.text();
            const imported = this._importRubricsFromJson(text);
            if (!imported.length) {
                this.state.rubricError = "No rubrics found in file.";
                return;
            }
            this.state.rubrics.push(...imported);
            await this._saveRubrics();
        } catch (e) {
            this.state.rubricError = "Invalid JSON: " + (e && e.message ? e.message : "could not parse file");
        } finally {
            input.value = "";
        }
    }

    _importRubricsFromJson(text) {
        const parsed = JSON.parse(text);
        let list = [];
        if (Array.isArray(parsed)) {
            list = parsed;
        } else if (parsed && typeof parsed === "object" && Array.isArray(parsed.rubrics)) {
            list = parsed.rubrics;
        } else {
            throw new Error("expected an array of rubrics or an object with a 'rubrics' array");
        }
        return list.map((r) => {
            const label = r.label || r.criterion || "";
            const score = typeof r.score === "number" ? r.score : 1;
            const is_positive = typeof r.is_positive === "boolean" ? r.is_positive : score > 0;
            const importance = r.importance || "important";
            let evaluation_target = r.evaluation_target || "state change";
            if (evaluation_target === "state_change") evaluation_target = "state change";
            return {
                label,
                claude: Array(12).fill(null),
                gpt: Array(12).fill(null),
                justification: { claude: "", gpt: "" },
                score,
                is_positive,
                type: r.type || "task completion",
                evaluation_target,
                importance,
            };
        }).filter((r) => r.label);
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
        const pass8 = rubric[model].slice(0, 8);
        return pass8.every(v => v === "fail");
    }

    onJustificationInput(rubricIndex, model, ev) {
        const rubric = this.state.rubrics[rubricIndex];
        if (!rubric) return;
        if (!rubric.justification) rubric.justification = { claude: "", gpt: "" };
        rubric.justification[model] = ev.target.value;
    }

    async onSaveJustification() {
        await this._saveRubrics();
    }

    async _loadTestResults() {
        if (!this.taskId) return;
        try {
            const sandboxIds = this.state.sandboxes.map(sb => sb.id).filter(Boolean);
            if (sandboxIds.length === 0) {
                this.state.testResults = {};
                return;
            }
            const results = await this.orm.searchRead(
                "kensei2.test.result",
                [["sandbox_id", "in", sandboxIds]],
                [
                    "id", "sandbox_id", "model_type", "model_used", "status",
                    "tests_total", "tests_passed", "tests_failed", "tests_errored",
                    "duration_generation_ms", "duration_execution_ms", "create_date",
                    "trajectory_index", "test_code", "test_output", "score", "test_scores",
                    "test_function_outputs",
                    "final_reward", "rubric_weights_percentage",
                    "average_rubric_weights_percentage",
                ],
                { order: "trajectory_index asc, create_date desc", limit: 100 },
            );
            const grouped = {};
            for (const r of results) {
                let modelType = "";
                if (r.sandbox_id) {
                    const sbId = r.sandbox_id[0];
                    const sb = this.state.sandboxes.find(s => s.id === sbId);
                    if (sb) modelType = sb.model_type;
                }
                if (!modelType) modelType = r.model_type || "unknown";
                if (!grouped[modelType]) grouped[modelType] = [];
                grouped[modelType].push(r);
            }
            this.state.testResults = grouped;
        } catch (e) {
            console.warn("[kensei2-dashboard] Failed to load test results:", e);
        }
    }

    async _loadTestWeightsStatus() {
        if (!this.taskId) return;
        try {
            const [data] = await this.orm.read(
                "kensei2.kensei2",
                [this.taskId],
                ["test_weights_status", "test_weights_error"],
            );
            this.state.testWeightsStatus = data.test_weights_status || "idle";
            this.state.testWeightsError = data.test_weights_error || "";
        } catch (e) {
            console.warn("[kensei2-dashboard] Failed to load test weights status:", e);
        }
    }

    async onGenerateTestWeights() {
        if (!this.taskId) return;
        try {
            this.state.testWeightsStatus = "generating";
            this.state.testWeightsError = "";
            await this.orm.call("kensei2.kensei2", "action_generate_test_weights", [[this.taskId]]);
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
                    await this._loadTestResults();
                } else if (this.state.testWeightsStatus === "error") {
                    this.notification.add("Test weight generation failed: " + this.state.testWeightsError, { type: "danger" });
                }
            }
        }, 5000);
    }

    async _loadStreamingStatuses() {
        if (!this.taskId) return;
        try {
            const [data] = await this.orm.read(
                "kensei2.kensei2",
                [this.taskId],
                [
                    "rubric_eval_status", "rubric_eval_error",
                    "rubric_test_overlap_status", "rubric_test_overlap_error", "rubric_test_overlap_report",
                    "golden_status", "golden_error",
                ],
            );
            this.state.rubricEvalStatus = data.rubric_eval_status || "idle";
            this.state.rubricEvalError = data.rubric_eval_error || "";
            this.state.rubricOverlapStatus = data.rubric_test_overlap_status || "idle";
            this.state.rubricOverlapError = data.rubric_test_overlap_error || "";
            this.state.rubricOverlapReport = data.rubric_test_overlap_report || "";
            this.state.goldenStreamStatus = data.golden_status === "generating" ? "idle" : (data.golden_status || "idle");
            this.state.goldenStreamError = data.golden_error || "";
        } catch (e) {
            console.warn("[kensei2-dashboard] Failed to load streaming statuses:", e);
        }
    }

    async _consumeSSE(url, body, onEvent) {
        const ctrl = new AbortController();
        let resp;
        try {
            resp = await fetch(url, {
                method: "POST",
                headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
                body: JSON.stringify(body),
                credentials: "same-origin",
                signal: ctrl.signal,
            });
        } catch (e) {
            onEvent({ type: "error", message: (e && e.message) || String(e) });
            return ctrl;
        }
        if (!resp.ok || !resp.body) {
            onEvent({ type: "error", message: `HTTP ${resp.status}` });
            return ctrl;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        try {
            // eslint-disable-next-line no-constant-condition
            while (true) {
                const { value, done } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let idx;
                while ((idx = buffer.indexOf("\n\n")) >= 0) {
                    const raw = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    const line = raw.startsWith("data: ") ? raw.slice(6) : raw;
                    if (!line) continue;
                    try {
                        onEvent(JSON.parse(line));
                    } catch (_e) {
                        // ignore malformed SSE line
                    }
                }
            }
        } catch (e) {
            if (e.name !== "AbortError") {
                onEvent({ type: "error", message: (e && e.message) || String(e) });
            }
        }
        return ctrl;
    }

    async onStreamGoldenTrajectory() {
        if (!this.taskId) return;
        if (this.state.goldenStreamStatus === "generating") return;
        this.state.goldenStreamStatus = "generating";
        this.state.goldenStreamError = "";
        this.state.goldenStreamText = "";
        this.notification.add("Streaming golden trajectory...", { type: "info" });
        this._goldenStreamAbort = await this._consumeSSE(
            "/kensei2/golden/stream",
            { record_id: this.taskId },
            (payload) => {
                if (payload.type === "delta" && payload.text) {
                    this.state.goldenStreamText += payload.text;
                } else if (payload.type === "error") {
                    this.state.goldenStreamStatus = "error";
                    this.state.goldenStreamError = payload.message || "Unknown error";
                    this.notification.add("Golden streaming failed: " + (payload.message || "error"), { type: "danger" });
                } else if (payload.type === "complete") {
                    this.state.goldenStreamStatus = payload.status === "error" ? "error" : "done";
                    if (payload.status !== "error") {
                        this.notification.add("Golden trajectory generated.", { type: "success" });
                        this.props.record.load();
                    }
                }
            },
        );
    }

    async onStreamRubricOverlap() {
        if (!this.taskId) return;
        if (this.state.rubricOverlapStatus === "generating") return;
        this.state.rubricOverlapStatus = "generating";
        this.state.rubricOverlapError = "";
        this.state.rubricOverlapReport = "";
        this.state.showRubricOverlapReport = true;
        this.notification.add("Streaming overlap audit...", { type: "info" });
        this._rubricOverlapAbort = await this._consumeSSE(
            "/kensei2/rubric_overlap/stream",
            { record_id: this.taskId },
            (payload) => {
                if (payload.type === "delta" && payload.text) {
                    this.state.rubricOverlapReport += payload.text;
                } else if (payload.type === "error") {
                    this.state.rubricOverlapStatus = "error";
                    this.state.rubricOverlapError = payload.message || "Unknown error";
                    this.notification.add("Overlap audit failed: " + (payload.message || "error"), { type: "danger" });
                } else if (payload.type === "complete") {
                    this.state.rubricOverlapStatus = payload.status === "error" ? "error" : "done";
                    if (payload.status !== "error") {
                        this.notification.add("Overlap audit complete.", { type: "success" });
                    }
                }
            },
        );
    }

    async onStreamRubricEval() {
        if (!this.taskId) return;
        if (this.state.rubricEvalStatus === "generating") return;
        if (!this.state.rubrics.length) {
            this.notification.add("No rubrics to evaluate. Add at least one rubric first.", { type: "warning" });
            return;
        }
        this.state.rubricEvalStatus = "generating";
        this.state.rubricEvalError = "";
        this.state.rubricEvalCompleted = 0;
        this.state.rubricEvalTotal = 0;
        this.notification.add("Evaluating rubrics across trajectories...", { type: "info" });
        this._rubricEvalAbort = await this._consumeSSE(
            "/kensei2/rubric_eval/stream",
            { record_id: this.taskId },
            (payload) => {
                if (payload.type === "start") {
                    this.state.rubricEvalTotal = payload.total_jobs || 0;
                } else if (payload.type === "job_complete" || payload.type === "job_error") {
                    this.state.rubricEvalCompleted = payload.completed || this.state.rubricEvalCompleted + 1;
                    this.state.rubricEvalTotal = payload.total || this.state.rubricEvalTotal;
                    // Live-merge verdicts into local rubric state so the user sees progress.
                    const verdicts = payload.verdicts || [];
                    const modelKey = payload.model;
                    const runIdx = payload.run;
                    if (typeof runIdx === "number" && modelKey) {
                        for (let r = 0; r < this.state.rubrics.length && r < verdicts.length; r++) {
                            const rub = this.state.rubrics[r];
                            if (!rub[modelKey] || !Array.isArray(rub[modelKey])) {
                                rub[modelKey] = new Array(12).fill(null);
                            }
                            while (rub[modelKey].length < 12) rub[modelKey].push(null);
                            rub[modelKey][runIdx] = verdicts[r];
                        }
                    }
                } else if (payload.type === "error") {
                    this.state.rubricEvalStatus = "error";
                    this.state.rubricEvalError = payload.message || "Unknown error";
                    this.notification.add("Rubric eval failed: " + (payload.message || "error"), { type: "danger" });
                } else if (payload.type === "complete") {
                    this.state.rubricEvalStatus = payload.status === "error" ? "error" : "done";
                    if (payload.status !== "error") {
                        this.notification.add("Rubric evaluation complete.", { type: "success" });
                        // Reload the record so the persisted rubrics overwrite the locally-mutated state.
                        this.props.record.load().then(() => this._loadRubrics());
                    }
                }
            },
        );
    }

    onToggleOverlapReport() {
        this.state.showRubricOverlapReport = !this.state.showRubricOverlapReport;
    }

    get activeTestResults() {
        return this.state.testResults[this.state.activeTrajectoryTab] || [];
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

    get testResultsSummary() {
        const results = this.activeTestResults;
        if (results.length === 0) return null;
        let totalTests = 0, passed = 0, failed = 0, errored = 0, running = 0;
        for (const r of results) {
            totalTests += r.tests_total || 0;
            passed += r.tests_passed || 0;
            failed += r.tests_failed || 0;
            errored += r.tests_errored || 0;
            if (r.status === "running" || r.status === "generating") running++;
        }
        return { total: totalTests, passed, failed, errored, running };
    }

    get rewardMetricsByModel() {
        return this.batchModels.map((m) => {
            const results = this.state.testResults[m.type] || [];
            if (results.length === 0) {
                return {
                    type: m.type, label: m.label, color: m.color,
                    runs: 0, current: null, percentage: null, average: null,
                };
            }
            const latest = [...results].sort(
                (a, b) => (b.trajectory_index || 0) - (a.trajectory_index || 0)
            )[0];
            const avgFromBackend = latest?.average_rubric_weights_percentage;
            const avg = (typeof avgFromBackend === "number" && avgFromBackend > 0)
                ? avgFromBackend
                : (results.reduce((s, r) => s + (r.rubric_weights_percentage || 0), 0) / results.length);
            return {
                type: m.type,
                label: m.label,
                color: m.color,
                runs: results.length,
                current: latest?.final_reward ?? null,
                percentage: latest?.rubric_weights_percentage ?? null,
                average: avg,
            };
        });
    }

    get consolidatedRewardMetrics() {
        const all = Object.values(this.state.testResults).flat();
        if (all.length === 0) {
            return { runs: 0, combinedAverage: null, bestModel: null, bestAverage: null };
        }
        const combinedAverage =
            all.reduce((s, r) => s + (r.rubric_weights_percentage || 0), 0) / all.length;
        const perModel = this.rewardMetricsByModel.filter((rm) => rm.runs > 0);
        let bestModel = null, bestAverage = null;
        for (const rm of perModel) {
            if (bestAverage === null || (rm.average ?? -1) > bestAverage) {
                bestAverage = rm.average;
                bestModel = rm.label;
            }
        }
        return { runs: all.length, combinedAverage, bestModel, bestAverage };
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
            await this.orm.write("kensei2.test.result", [resultId], { score });
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
            await this.orm.write("kensei2.test.result", [resultId], { test_scores: value });
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

    _getFunctionOverrideOutput(result, funcName) {
        // Reads optional per-function override output left over from the
        // (now-removed) retry feature. Older test.result rows may still have
        // entries in test_function_outputs; we keep honoring them so historical
        // data doesn't suddenly read differently.
        try {
            const overrides = JSON.parse(result.test_function_outputs || "{}");
            return overrides[funcName] || null;
        } catch (_e) {
            return null;
        }
    }

    getTestFunctions(result) {
        if (!result.test_code) return [];
        const regex = /(?:def|async def)\s+(test_\w+)\s*\(/gm;
        const funcs = [];
        let match;
        while ((match = regex.exec(result.test_code)) !== null) {
            funcs.push(match[1]);
        }
        return funcs;
    }

    getFunctionStatus(result, funcName) {
        const override = this._getFunctionOverrideOutput(result, funcName);
        const output = override || result.test_output;
        if (!output) return "unknown";
        if (output.includes(funcName + " PASSED") || output.match(new RegExp(funcName + "\\s+PASSED"))) return "passed";
        if (output.includes(funcName + " FAILED") || output.match(new RegExp(funcName + "\\s+FAILED"))) return "failed";
        if (output.includes(funcName + " ERROR") || output.match(new RegExp(funcName + "\\s+ERROR"))) return "error";
        return "unknown";
    }

    getFunctionOutput(result, funcName) {
        const override = this._getFunctionOverrideOutput(result, funcName);
        if (override) return override.trim();
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
        const regex = new RegExp(`([ \\t]*(?:def|async def)\\s+${funcName}\\s*\\([\\s\\S]*?)(?=\\n[ \\t]*(?:def|async def|class)\\s|$)`, "m");
        const match = code.match(regex);
        return match ? match[1].trim() : "";
    }
}

export const taskDashboardDef = { component: TaskDashboard };
registry.category("view_widgets").add("kensei2_task_dashboard", taskDashboardDef);
